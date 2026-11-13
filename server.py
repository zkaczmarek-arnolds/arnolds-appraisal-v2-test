import os, re, datetime, io, json, hashlib, mimetypes
from flask import Flask, request, jsonify, send_from_directory
from google.cloud import firestore
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google.auth import default as google_auth_default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

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
    # Existing bootstrap users also receive these defaults; other employees
    # use their configured Firestore user profile values.
    if email==BOOTSTRAP_ADMIN:
        u.setdefault('homeStore','Glencoe')
        u.setdefault('dmsInitials','ZK1')
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
    store=u.get('homeStore','')
    if store not in STORES or (u['role']!='System Admin' and store not in locations):
        store=locations[0] if len(locations)==1 else ''
    if not store:
        return fail('Set a valid home store in the employee profile before creating an appraisal')
    initials=str(u.get('dmsInitials','')).strip()[:20]
    r={'id':id,'status':status,'form':body['form'],'formName':str(body.get('formName',''))[:250], 'version':1,'salesEmail':u['email'],'salesName':u.get('name',u['email']),'initials':initials,'store':store,'created':now,'updated':now,'serial':'','make':'','model':'','customer':'','stock':'','data':{'salesinitials':initials,'location':store},'revisions':0,'audit':[{'at':now,'by':u['email'],'action':'BLANK_PRINTED' if body['mode']=='blank' else 'CREATED'}]}
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


# TEST-only destination. Set to the ID of the EXISTING "TEST - Equipment Appraisals" folder.
DRIVE_ROOT_FOLDER_ID=os.environ.get('DRIVE_TEST_FOLDER_ID','').strip()
MAX_EXPORT_BYTES=28*1024*1024

def drive_service():
    if not DRIVE_ROOT_FOLDER_ID:
        raise RuntimeError('DRIVE_TEST_FOLDER_ID is not configured')
    credentials,_=google_auth_default(scopes=['https://www.googleapis.com/auth/drive'])
    return build('drive','v3',credentials=credentials,cache_discovery=False)

def drive_child(service,parent,name):
    escaped=name.replace("'", "\\'")
    result=service.files().list(q=f"'{parent}' in parents and name = '{escaped}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",fields='files(id,name),nextPageToken',page_size=100,supportsAllDrives=True,includeItemsFromAllDrives=True).execute()
    if result.get('files'): return result['files'][0]['id']
    obj=service.files().create(body={'name':name,'mimeType':'application/vnd.google-apps.folder','parents':[parent]},fields='id',supportsAllDrives=True).execute()
    return obj['id']

def drive_upload(service,parent,name,data,mime):
    # A retry replaces only the same revision's file, avoiding duplicate exports.
    escaped=name.replace("'", "\\'")
    found=service.files().list(q=f"'{parent}' in parents and name = '{escaped}' and trashed = false",fields='files(id,name)',page_size=100,supportsAllDrives=True,includeItemsFromAllDrives=True).execute().get('files',[])
    media=MediaIoBaseUpload(io.BytesIO(data),mimetype=mime,resumable=False)
    if found:
        return service.files().update(fileId=found[0]['id'],media_body=media,fields='id,webViewLink',supportsAllDrives=True).execute()
    return service.files().create(body={'name':name,'parents':[parent]},media_body=media,fields='id,webViewLink',supportsAllDrives=True).execute()

@app.post('/api/records/<record_id>/drive-export')
def export_submitted(record_id):
    u=user()
    if not re.fullmatch(r'\d{2}-\d{5,}',record_id):return fail('Invalid appraisal number')
    snap=collection('records').document(record_id).get()
    if not snap.exists:return fail('Appraisal not found',404)
    r=snap.to_dict()
    if not visible(u,r):raise PermissionError('Not allowed to view this appraisal')
    if not editable(u,r):raise PermissionError('Not allowed to export this appraisal')
    if r.get('status')!='Submitted':return fail('Only submitted appraisals can be exported')
    if not DRIVE_ROOT_FOLDER_ID:return fail('TEST Google Drive destination has not been configured',503)
    try:
        revision=int(request.form.get('revision','-1'))
        if revision!=int(r.get('revisions',0)):return fail('Appraisal revision changed; reopen and retry export',409)
        clean=request.files.get('appraisal_only')
        combined=request.files.get('complete_appraisal')
        if not clean or not combined:return fail('Both appraisal PDFs are required')
        clean_bytes=clean.read(); combined_bytes=combined.read()
        if not clean_bytes.startswith(b'%PDF-') or not combined_bytes.startswith(b'%PDF-'):return fail('Invalid PDF file')
        entries=[]
        for f in request.files.getlist('photos'):
            raw=f.read();mime=f.mimetype or ''
            if not mime.startswith('image/') or not raw:return fail('Invalid equipment photo')
            entries.append((f,raw,mime))
        for f in request.files.getlist('internal_photos'):
            raw=f.read();mime=f.mimetype or ''
            if not mime.startswith('image/') or not raw:return fail('Invalid internal photo')
            entries.append((f,raw,mime))
        for f in request.files.getlist('supporting_documents'):
            raw=f.read();mime=f.mimetype or ''
            if not (mime.startswith('image/') or mime=='application/pdf') or not raw:return fail('Invalid supporting document')
            entries.append((f,raw,mime))
        total=len(clean_bytes)+len(combined_bytes)+sum(len(raw) for _,raw,_ in entries)
        if total>MAX_EXPORT_BYTES:return fail('Export exceeds 28 MB. Reduce photo sizes and retry.',413)
        if len(entries)>70:return fail('Too many attachments')
        # Do not overwrite prior exports; a unique revision folder preserves history.
        service=drive_service()
        store=r.get('store')
        if store not in STORES:return fail('Invalid appraisal store')
        year='20'+record_id[:2]
        parent=drive_child(service,DRIVE_ROOT_FOLDER_ID,store)
        parent=drive_child(service,parent,year)
        parent=drive_child(service,parent,record_id)
        revision_name='Revision '+str(revision)
        # An existing revision folder is reused on retry, but each upload creates
        # a new file. This prevents silent replacement of a prior document.
        rev_folder=drive_child(service,parent,revision_name)
        drive_upload(service,rev_folder,f'{record_id} - Appraisal Only.pdf',clean_bytes,'application/pdf')
        drive_upload(service,rev_folder,f'{record_id} - Complete Appraisal.pdf',combined_bytes,'application/pdf')
        photos_folder=drive_child(service,rev_folder,'Photos')
        internal_folder=None
        support_folder=None
        for category in ('photos','internal_photos','supporting_documents'):
            for i,f in enumerate(request.files.getlist(category),1):
                # Safe, stable names; never use untrusted path fragments.
                extension=os.path.splitext(f.filename or '')[1].lower()
                if extension not in ('.jpg','.jpeg','.png','.webp','.heic','.pdf'):
                    extension='.pdf' if f.mimetype=='application/pdf' else '.jpg'
                if category=='photos':dest=photos_folder
                elif category=='internal_photos':
                    if internal_folder is None:internal_folder=drive_child(service,rev_folder,'Internal Photos - Restricted')
                    dest=internal_folder
                else:
                    if support_folder is None:support_folder=drive_child(service,rev_folder,'Supporting Documents - Restricted')
                    dest=support_folder
                # Same bytes were validated above; file pointers reset after read.
                f.stream.seek(0)
                drive_upload(service,dest,f'{category}-{i:02d}{extension}',f.read(),f.mimetype or 'application/octet-stream')
        link='https://drive.google.com/drive/folders/'+parent
        collection('records').document(record_id).update({'driveExport':{'revision':revision,'folderId':parent,'revisionFolderId':rev_folder,'url':link,'exportedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'exportedBy':u['email']}})
        return jsonify(ok=True,folderUrl=link,revision=revision)
    except Exception:
        app.logger.exception('Google Drive export failed for %s',record_id)
        return fail('Google Drive export failed. Appraisal remains submitted in Firestore. Retry export from the appraisal page.',502)

@app.get('/api/health')
def health():return jsonify(status='ok',environment='TEST')

if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
