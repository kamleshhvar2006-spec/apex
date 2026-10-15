# APEX Logistics on Zoho Catalyst

Web client and API both live on one Catalyst domain:
- Client:   https://<project-domain>/app/
- Function: https://<project-domain>/server/apex_api/   (client/index.html already points at `/server/apex_api`)

## 1. Create the project skeleton with the CLI
    npm install -g zcatalyst-cli
    catalyst login
    catalyst init            # pick: Functions + Client
    #   Function: type Advanced I/O, language Python, name  apex_api
    #   Client:   name apex  (hosting)
Then copy my files over the generated ones (do NOT overwrite catalyst-config.json / catalyst.json):
- functions/apex_api/main.py and requirements.txt  ->  your functions/apex_api/
- client/index.html                               ->  your client/

## 2. Create two tables in Console > Cloud Scale > Data Store
Column names are case-sensitive.

Shipments (all Var Char unless noted; make trackingId Mandatory + Unique)
  trackingId, status, customerName, email, origin, destination, currentLocation, mode,
  estimatedDelivery, createdAt, updatedAt, weightKg (Double), estimatedCost (Big Int)

Fleet (all Var Char)
  vehicleId, type, name, status, location

Then import data/Fleet.csv into the Fleet table (Import option on the table page).

## 3. Admin login settings
    python scripts/make_admin_hash.py you@example.com
It prints ADMIN_EMAIL, ADMIN_PASSWORD_HASH and TOKEN_SECRET. Add all three as environment
variables of the apex_api function (Console > Functions > apex_api). If your function page has no
environment variable option, create functions/apex_api/config.py with the same three names as
Python strings (ADMIN_EMAIL = "...") - never commit that file.

## 4. Test and deploy
    catalyst serve           # local test, client on http://localhost:3000/app/
    catalyst deploy          # pushes to the Development environment
Open the Web Client URL shown in the console. Book a shipment, track it, then sign in from the
Admin section with the email and password you chose in step 3.

## Notes
- Development environment allows 5,000 rows per table. Move to Production from the console before launch.
- Dashboard reads rows 300 at a time (ZCQL limit) and sums in the function; fine for thousands of shipments.
- To go back to the in-browser demo, set API_BASE to '' in client/index.html.
