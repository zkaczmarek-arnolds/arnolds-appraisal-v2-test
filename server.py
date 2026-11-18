import os, re, datetime, io, json, hashlib, mimetypes
from flask import Flask, request, jsonify, send_from_directory
from google.cloud import firestore
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials as OAuthCredentials
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
    # Client-generated key identifies one logical creation, even across retries.
    request_id=str(body.get('requestId','')).strip()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{12,100}',request_id):
        return fail('Missing or invalid request ID')
    # Bind request keys to the authenticated employee; avoid cross-user collisions.
    import hashlib
    key=hashlib.sha256((u['email']+'|'+request_id).encode()).hexdigest()
    locations=u.get('locations',[])
    store=u.get('homeStore','')
    if store not in STORES or (u['role']!='System Admin' and store not in locations):
        store=locations[0] if len(locations)==1 else ''
    if not store:
        return fail('Set a valid home store in the employee profile before creating an appraisal')
    initials=str(u.get('dmsInitials','')).strip()[:20]
    @firestore.transactional
    def create_once(tx):
        idem_ref=collection('creation_requests').document(key)
        previous=idem_ref.get(transaction=tx)
        if previous.exists:
            meta=previous.to_dict()
            if meta.get('mode')!=body['mode'] or meta.get('form')!=body['form']:
                raise ValueError('Request ID was reused for a different appraisal')
            existing_ref=collection('records').document(meta['recordId'])
            existing=existing_ref.get(transaction=tx)
            if not existing.exists:
                raise ValueError('Previously created appraisal is unavailable')
            return existing.to_dict(),False
        counter=collection('counters').document('appraisals')
        snap=counter.get(transaction=tx)
        seq=((snap.to_dict() or {}).get('seq',0)+1) if snap.exists else 1
        now=datetime.datetime.now(datetime.timezone.utc).isoformat()
        record_id=f'{datetime.datetime.now().year%100:02d}-{seq:05d}'
        status='Blank Printed' if body['mode']=='blank' else 'Draft'
        r={'id':record_id,'status':status,'form':body['form'],'formName':str(body.get('formName',''))[:250], 'version':1,'salesEmail':u['email'],'salesName':u.get('name',u['email']),'initials':initials,'store':store,'created':now,'updated':now,'serial':'','make':'','model':'','customer':'','stock':'','data':{'salesinitials':initials,'location':store},'revisions':0,'audit':[{'at':now,'by':u['email'],'action':'BLANK_PRINTED' if body['mode']=='blank' else 'CREATED'}]}
        tx.set(counter,{'seq':seq})
        tx.create(collection('records').document(record_id),r)
        tx.create(idem_ref,{'recordId':record_id,'mode':body['mode'],'form':body['form'],'created':now,'email':u['email']})
        return r,True
    try:
        record,created=create_once(client.transaction())
    except ValueError as e:
        return fail(str(e))
    return jsonify(record=record,created=created),(201 if created else 200)

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
    """Use the dedicated Workspace user's OAuth refresh token from Secret Manager.

    Secret JSON is mounted as an environment variable by Cloud Run. Never log it.
    The dedicated account should have access only to the TEST folder.
    """
    if not DRIVE_ROOT_FOLDER_ID:
        raise RuntimeError('DRIVE_TEST_FOLDER_ID is not configured')
    secret=(os.environ.get('GOOGLE_OAUTH_SECRET_JSON') or os.environ.get('DRIVE_OAUTH_CREDENTIALS_JSON') or '').strip()
    if not secret:
        raise RuntimeError('GOOGLE_OAUTH_SECRET_JSON secret is not configured')
    try:
        config=json.loads(secret)
        required=('client_id','client_secret','refresh_token')
        if not all(config.get(k) for k in required):
            raise ValueError('OAuth credentials are missing required fields')
        credentials=OAuthCredentials(
            token=None,
            refresh_token=config['refresh_token'],
            token_uri='https://oauth2.googleapis.com/token',
            client_id=config['client_id'],
            client_secret=config['client_secret'],
            scopes=['https://www.googleapis.com/auth/drive'],
        )
        credentials.refresh(Request())
    except Exception as exc:
        raise RuntimeError('Google Drive OAuth credentials could not be refreshed') from exc
    return build('drive','v3',credentials=credentials,cache_discovery=False)

def drive_child(service,parent,name):
    escaped=name.replace("'", "\\'")
    result=service.files().list(q=f"'{parent}' in parents and name = '{escaped}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",fields='files(id,name),nextPageToken',pageSize=100,supportsAllDrives=True,includeItemsFromAllDrives=True).execute()
    if result.get('files'): return result['files'][0]['id']
    obj=service.files().create(body={'name':name,'mimeType':'application/vnd.google-apps.folder','parents':[parent]},fields='id',supportsAllDrives=True).execute()
    return obj['id']

def drive_upload(service,parent,name,data,mime):
    # A retry replaces only the same revision's file, avoiding duplicate exports.
    escaped=name.replace("'", "\\'")
    found=service.files().list(q=f"'{parent}' in parents and name = '{escaped}' and trashed = false",fields='files(id,name)',pageSize=100,supportsAllDrives=True,includeItemsFromAllDrives=True).execute().get('files',[])
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
        # TEST folder layout: Store / Salesperson - Initials / ID - Make Model.
        # Latest export lives at the appraisal root; older submissions are archived.
        service=drive_service()
        store=r.get('store')
        if store not in STORES:return fail('Invalid appraisal store')
        def safe_name(value, fallback, limit=90):
            value=re.sub(r'[\\/<>:"|?*\x00-\x1f]+',' ',str(value or '')).strip(' .')
            return (value[:limit].strip(' .') or fallback)
        salesperson=safe_name(r.get('salesName') or r.get('salesEmail'),'Unknown Salesperson')
        initials=safe_name(r.get('initials') or (r.get('data') or {}).get('salesinitials'),'No Initials',20)
        equipment=' '.join(x for x in (safe_name(r.get('make'),'',45),safe_name(r.get('model'),'',45)) if x).strip()
        appraisal_name=record_id+(' - '+equipment if equipment else '')
        store_folder=drive_child(service,DRIVE_ROOT_FOLDER_ID,store)
        sales_folder=drive_child(service,store_folder,salesperson+' - '+initials)
        parent=drive_child(service,sales_folder,appraisal_name)

        def list_children(folder):
            found=[];page=None
            while True:
                args=dict(q=f"'{folder}' in parents and trashed = false",fields='nextPageToken,files(id,name,mimeType)',pageSize=100,supportsAllDrives=True,includeItemsFromAllDrives=True)
                if page:args['pageToken']=page
                result=service.files().list(**args).execute()
                found.extend(result.get('files',[]));page=result.get('nextPageToken')
                if not page:break
            return found

        def move_item(item_id,src,dst):
            service.files().update(fileId=item_id,addParents=dst,removeParents=src,fields='id,parents',supportsAllDrives=True).execute()

        # A staging folder prevents incomplete uploads from replacing a usable appraisal.
        # Retrying an export reuses this staging folder and never creates a new version.
        stage=drive_child(service,parent,'_Export In Progress')
        drive_upload(service,stage,f'{record_id} - Appraisal Only.pdf',clean_bytes,'application/pdf')
        drive_upload(service,stage,f'{record_id} - Complete Appraisal.pdf',combined_bytes,'application/pdf')
        photos_folder=drive_child(service,stage,'Equipment Photos')
        internal_folder=None
        support_folder=None
        for category in ('photos','internal_photos','supporting_documents'):
            for i,f in enumerate(request.files.getlist(category),1):
                extension=os.path.splitext(f.filename or '')[1].lower()
                if extension not in ('.jpg','.jpeg','.png','.webp','.heic','.pdf'):
                    extension='.pdf' if f.mimetype=='application/pdf' else '.jpg'
                if category=='photos':dest=photos_folder
                elif category=='internal_photos':
                    if internal_folder is None:internal_folder=drive_child(service,stage,'Internal Photos & Documents')
                    dest=internal_folder
                else:
                    if support_folder is None:support_folder=drive_child(service,stage,'Internal Photos & Documents')
                    dest=support_folder
                f.stream.seek(0)
                drive_upload(service,dest,f'{category}-{i:02d}{extension}',f.read(),f.mimetype or 'application/octet-stream')

        export_info=r.get('driveExport') or {}
        previous_revision=int(export_info.get('revision',0) or 0)
        # Archive only when a NEW revision is being exported; a retry is not a resubmission.
        previous_items=[item for item in list_children(parent) if item['name'] not in ('_Export In Progress','Archived Versions')]
        if previous_items and revision>previous_revision:
            archive=drive_child(service,parent,'Archived Versions')
            old_version=drive_child(service,archive,'Version '+str(previous_revision or max(1,revision-1)))
            for item in previous_items:move_item(item['id'],parent,old_version)
        elif previous_items and revision==previous_revision:
            # Retry: replace the files for this same version, without creating history.
            # Preserve an existing file until the new staging upload has completed.
            for item in previous_items:
                service.files().delete(fileId=item['id'],supportsAllDrives=True).execute()
        elif previous_items:
            return fail('A newer export already exists. Reopen the appraisal before exporting.',409)
        for item in list_children(stage):move_item(item['id'],stage,parent)
        service.files().delete(fileId=stage,supportsAllDrives=True).execute()
        link='https://drive.google.com/drive/folders/'+parent
        collection('records').document(record_id).update({'driveExport':{'revision':revision,'folderId':parent,'url':link,'exportedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'exportedBy':u['email']}})
        return jsonify(ok=True,folderUrl=link,revision=revision)
    except Exception:
        app.logger.exception('Google Drive export failed for %s',record_id)
        return fail('Google Drive export failed. Appraisal remains submitted in Firestore. Retry export from the appraisal page.',502)



# Help-only AI pilot. Authentication is required; no appraisal record data is sent.
@app.post('/api/support/tickets')
def submit_support_ticket():
    """Append to existing IT ticket sheet; requires Sheets API and service-account sheet access."""
    try:
        u=user()
    except PermissionError as exc:
        return fail(str(exc),401)
    payload=request.get_json(silent=True) or {}
    title=str(payload.get('title','')).strip()
    details=str(payload.get('details','')).strip()
    if not title or not details or len(title)>150 or len(details)>3000:
        return fail('Issue title and details are required (150 / 3000 character limits).')
    spreadsheet_id=os.environ.get('IT_TICKET_SPREADSHEET_ID','').strip()
    sheet_tab=os.environ.get('IT_TICKET_SHEET_TAB','').strip()
    if not spreadsheet_id or not sheet_tab:
        return fail('Ticket submission is not configured yet. Please contact IT.',503)
    # Columns B:J: timestamp, name, email, title, details, email address,
    # assignment, status, solution. No user-controlled formula execution.
    def safe_cell(value):
        value=str(value)
        return "'"+value if value.startswith(('=','+','-','@')) else value
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    row=[now,safe_cell(u.get('name',u['email'])),u['email'],safe_cell(title),safe_cell(details),u['email'],'','New','']
    try:
        import google.auth
        credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets'])
        sheets=build('sheets','v4',credentials=credentials,cache_discovery=False)
        escaped=sheet_tab.replace("'","''")
        result=sheets.spreadsheets().values().append(spreadsheetId=spreadsheet_id,
            range=f"'{escaped}'!B:J",valueInputOption='RAW',insertDataOption='INSERT_ROWS',
            body={'values':[row]}).execute()
        return jsonify(ok=True,updatedRange=result.get('updates',{}).get('updatedRange',''))
    except Exception:
        app.logger.exception('IT ticket append failed')
        return fail('Could not save ticket to the IT sheet. Please contact IT directly.',502)

@app.post('/api/support/ask')
def support_ask():
    try:
        u=identity()
    except Exception:
        return fail('Sign in to use the support assistant.',401)
    payload=request.get_json(silent=True) or {}
    question=str(payload.get('question','')).strip()
    if not question or len(question)>1000:
        return fail('Enter a question up to 1000 characters.')
    api_key=os.environ.get('OPENAI_API_KEY','')
    if not api_key:
        return jsonify(answer='The AI assistant has not been configured yet. Please use the How-To tab or Report Issue to contact IT.',configured=False)
    import requests
    knowledge=(
        'You are a read-only help assistant for Arnold Companies Equipment Appraisal TEST system. '
        'Answer only how-to questions about the appraisal workflow. Never request passwords or sensitive customer information. '
        'If unsure, say so and direct the user to Report Issue. Never claim to have accessed a live appraisal or changed records. '
        'Known workflow: Save Draft saves to Firestore without Drive export; Submit creates a submitted revision; '
        'Google Drive export organizes files by store, salesperson, appraisal folder; latest exports in main folder, '
        'older submitted versions in Archived Versions. Google Drive export may fail separately from successful submission. '
        'Manager approval is not confirmed as implemented. Do not claim that it is.'
    )
    try:
        response=requests.post('https://api.openai.com/v1/responses',headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},json={
            'model':os.environ.get('OPENAI_SUPPORT_MODEL','gpt-4.1-mini'),
            'instructions':knowledge,
            'input':question,
            'max_output_tokens':350,
            'store':False
        },timeout=25)
        response.raise_for_status()
        data=response.json()
        parts=[c.get('text','') for item in data.get('output',[]) for c in item.get('content',[]) if c.get('type')=='output_text']
        answer='\n'.join(parts).strip()
        return jsonify(answer=answer or 'I could not generate an answer. Please report the issue to IT.',configured=True)
    except Exception:
        app.logger.exception('Support AI request failed')
        return fail('AI support is temporarily unavailable. Please use Report Issue.',502)

@app.get('/api/health')
def health():return jsonify(status='ok',environment='TEST')

if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
