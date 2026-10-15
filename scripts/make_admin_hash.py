"""Create the three admin settings.  Usage: python scripts/make_admin_hash.py you@example.com"""
import base64, getpass, hashlib, secrets, sys

if len(sys.argv) != 2:
    sys.exit(__doc__)
b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
pw, salt, iters = getpass.getpass("Choose admin password (10+ chars): "), secrets.token_bytes(16), 200_000
if len(pw) < 10:
    sys.exit("Password too short.")
print("\nSet these as environment variables on the apex_api function:\n")
print(f"ADMIN_EMAIL={sys.argv[1]}")
print(f"ADMIN_PASSWORD_HASH=pbkdf2${iters}${b(salt)}${b(hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, iters))}")
print(f"TOKEN_SECRET={secrets.token_urlsafe(48)}")
