import argparse
import hashlib
import os

HTPASSWD_FILE = "admin.htpasswd"

def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    pass_hash = hashlib.sha256((salt + password).encode('utf-8')).hexdigest()
    return f"{salt}${pass_hash}"

def create_admin(username, password):
    new_hash = hash_password(password)
    
    lines = []
    if os.path.exists(HTPASSWD_FILE):
        with open(HTPASSWD_FILE, "r") as f:
            lines = f.readlines()
            
    # Check if user exists and replace, else append
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(f"{username}:"):
            lines[i] = f"{username}:{new_hash}\n"
            replaced = True
            break
            
    if not replaced:
        lines.append(f"{username}:{new_hash}\n")
        
    with open(HTPASSWD_FILE, "w") as f:
        f.writelines(lines)
        
    print(f"User '{username}' successfully added/updated in {HTPASSWD_FILE}")

def main():
    parser = argparse.ArgumentParser(description="Generate admin.htpasswd file for Admin UI basic auth")
    parser.add_argument("username", help="Admin username")
    parser.add_argument("password", help="Admin password")
    
    args = parser.parse_args()
    create_admin(args.username, args.password)

if __name__ == "__main__":
    main()
