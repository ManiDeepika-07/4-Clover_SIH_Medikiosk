import re

def secure_filename(name):
    name=name.replace('\\','/').split('/')[-1]
    name=re.sub(r'[^A-Za-z0-9._-]+','_',name).strip('._')
    return name or 'upload'
