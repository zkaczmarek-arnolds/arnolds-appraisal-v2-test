import os, re, datetime
from flask import Flask, request, jsonify, send_from_directory
from google.cloud import firestore
from google.auth.transport.requests import Request
from google.oauth2 import id_token

app=Flask(__name__,static_folder=None)
client=firestore.Client()
AUDIENCE=os.environ.get('IAP_AUDIENCE','')
COLLECTION_PREFIX=os.environ.get('COLLECTION_PREFIX','test_')
ALLOWED_DOMAIN=os.environ.get('ALLOWED_DOMAIN','arnoldsinc.com').lower()
BOOTSTRAP_ADMIN=os.environ.get('BOOTSTRAP_ADMIN_EMAIL','').lower()
STORES=['St. Cloud','St. Martin','Kimball','Glencoe','Willmar','Mankato','Alden','Eyota']
ROLES=['Salesperson','Store Admin','Store Manager','System Admin']

def collection(name): return client.collection(COLLECTION_PREFIX+name)
def fail(message,code=400): return jsonify(error=message),code

def identity():
    if not AUDIENCE: raise PermissionError('IAP_AUDIENCE is not configured')
    token=request.headers.get('X-Goog-IAP-JWT-Assertion','')
    if not token: raise PermissionError('IAP identity missing; use the protected Cloud Run URL')
    claims=id_token.verify_token(token,Request(),audience=AUDIENCE,certs_url='https://www.gstatic.com/iap/verify/public_key')
    email=str(claims.get('email','')).lower()
    if not email.endswith('@'+ALLOWED_DOMAIN): raise PermissionError('Company Google account required')
    return email

def user():
    email=identity()
    doc=collection('users').document(email).get()
    if doc.exists:
        u=doc.to_dict()
        if not u.get('active',False): raise PermissionError('Account disabled')
    elif email==BOOTSTRAP_ADMIN:
        u={'email':email,'name':email.split('@')[0],'role':'System Admin','locations':STORES,'active':True}
        collection('users').document(email).set(u)
    else: raise PermissionError('Account is not provisioned in TEST')
    u['email']=email
    return u

def visible(u,r):
    return u['role']=='System Admin' or (u['role']=='Salesperson' and r.get('salesEmail','').lower()==u['email']) or (u['role'] in ('Store Admin','Store Manager') and r.get('store') in u.get('locations',[]))

def editable(u,r):
    return u['role']=='System Admin' or (u['role']=='Salesperson' and r.get('salesEmail','').lower()==u['email']) or (u['role']=='Store Admin' and r.get('store') in u.get('locations',[]))

@app.errorhandler(PermissionError)
def forbidden(e):return fail(str(e),403)
@app.errorhandler(Exception)
def unexpected(e):
    app.logger.exception('Request failed')
    return fail('Server request failed. Check Cloud Run logs.',500)

@app.get('/')
def index():return send_from_directory('.','index.html')
@app.get('/api/bootstrap')
def bootstrap():
    u=user()
    records=[s.to_dict() for s in collection('records').stream() if visible(u,s.to_dict())]
    return jsonify(user=u,records=records)

@app.post('/api/records')
def create():
    u=user()
    if u['role']=='Store Manager':return fail('Store managers cannot create appraisals',403)
    body=request.get_json(silent=True) or {}
    if body.get('mode') not in ('draft','blank'):return fail('Invalid mode')
    if not isinstance(body.get('form'),int) or body['form']<0:return fail('Invalid form')
    @firestore.transactional
    def reserve(tx):
        ref=collection('counters').document('appraisals')
        snap=ref.get(transaction=tx)
        seq=(snap.to_dict() or {}).get('seq',0)+1 if snap.exists else 1
        tx.set(ref,{'seq':seq})
        return seq
    seq=reserve(client.transaction())
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    id=f'{datetime.datetime.now().year%100:02d}-{seq:05d}'
    status='Blank Printed' if body['mode']=='blank' else 'Draft'
    locations=u.get('locations',[])
    store=locations[0] if len(locations)==1 else ''
    r={'id':id,'status':status,'form':body['form'],'formName':str(body.get('formName',''))[:250], 'version':1,'salesEmail':u['email'],'salesName':u.get('name',u['email']),'initials':'','store':store,'created':now,'updated':now,'serial':'','make':'','model':'','customer':'','stock':'','data':{'salesinitials':'','location':store},'revisions':0,'audit':[{'at':now,'by':u['email'],'action':'BLANK_PRINTED' if body['mode']=='blank' else 'CREATED'}]}
    collection('records').document(id).create(r)
    return jsonify(record=r),201

@app.put('/api/records/<record_id>')
def update(record_id):
    u=user()
    if not re.fullmatch(r'\d{2}-\d{5,}',record_id):return fail('Invalid appraisal number')
    body=request.get_json(silent=True) or {}
    ref=collection('records').document(record_id)
    @firestore.transactional
    def apply(tx):
        snap=ref.get(transaction=tx)
        if not snap.exists:raise ValueError('Appraisal not found')
        old=snap.to_dict()
        if not visible(u,old) or not editable(u,old):raise PermissionError('Not allowed to edit this appraisal')
        if old['status']=='Finalized' and u['role']!='System Admin':raise PermissionError('Finalized appraisals are locked')
        newstatus=body.get('status',old['status'])
        transitions={'Draft':{'Draft','Submitted','Unused'},'Blank Printed':{'Blank Printed','Draft','Unused'},'Submitted':{'Submitted','Finalized','Draft'},'Finalized':{'Finalized','Submitted'},'Unused':{'Unused'},'Void':{'Void'}}
        if newstatus not in transitions.get(old['status'],set()):raise ValueError('Invalid status transition')
        if newstatus=='Finalized' and u['role'] not in ('Store Admin','System Admin'):raise PermissionError('Manager or admin finalization required')
        if newstatus=='Submitted' and not str(body.get('serial',old.get('serial',''))).strip():raise ValueError('Serial number required')
        if newstatus=='Finalized' and not str(body.get('customer',old.get('customer',''))).strip():raise ValueError('Customer required')
        allowed=('status','serial','make','model','customer','stock','data','initials','store','revisions')
        updates={k:body[k] for k in allowed if k in body}
        if 'data' in updates and (not isinstance(updates['data'],dict) or len(str(updates['data']))>250000):raise ValueError('Invalid form data')
        if 'store' in updates and updates['store'] not in STORES:raise ValueError('Invalid store')
        if u['role']!='System Admin' and 'store' in updates and updates['store'] not in u.get('locations',[]):raise PermissionError('Store is not assigned to user')
        if u['role']!='System Admin' and u['role']!='Salesperson' and old.get('store') not in u.get('locations',[]):raise PermissionError('Store not permitted')
        updates['updated']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        audit=old.get('audit',[])
        audit.append({'at':updates['updated'],'by':u['email'],'action':'STATUS_'+newstatus if newstatus!=old['status'] else 'UPDATED'})
        updates['audit']=audit[-500:]
        tx.update(ref,updates)
        old.update(updates)
        return old
    try:return jsonify(record=apply(client.transaction()))
    except ValueError as e:return fail(str(e))

@app.get('/api/health')
def health():return jsonify(status='ok',environment='TEST')

if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
