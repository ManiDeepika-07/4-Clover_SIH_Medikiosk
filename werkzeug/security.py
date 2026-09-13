import hashlib, hmac, os, base64

def generate_password_hash(password):
    salt=os.urandom(16); dk=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,200000)
    return 'pbkdf2:sha256:200000$'+base64.urlsafe_b64encode(salt).decode()+'$'+base64.urlsafe_b64encode(dk).decode()

def check_password_hash(stored,password):
    try:
        _, rest=stored.split('pbkdf2:sha256:200000$',1); salt_b64,hash_b64=rest.split('$',1)
        salt=base64.urlsafe_b64decode(salt_b64.encode()); dk=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,200000)
        return hmac.compare_digest(base64.urlsafe_b64encode(dk).decode(),hash_b64)
    except Exception: return False
