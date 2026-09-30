# Firebase Hosting, as a front door for a custom domain

Cloud Run offers no domain mapping in `europe-west3`, and the console says so: *"Domain mappings are
not available in the region of the selected service. Either copy this service to a different region,
use an Application Load Balancer or Firebase Hosting."* Copying the service away from its database is
the wrong trade (see `infra/variables.tf` on why the region is deliberate), and a load balancer's
forwarding rule costs more per month than the rest of this stack. So: Hosting, as a pure proxy.

## Why `hosting/public` is empty, and must stay empty

Firebase Hosting serves a static file in preference to a rewrite. An `index.html` in this directory
would therefore be served at `/` and the rewrite below it would never run for the one path that
matters most - the site would answer with a blank page and everything *except* the home page would
work, which is a confusing way to find out.

So the directory holds nothing but `.gitkeep`, which exists only because git does not track empty
directories, and which `ignore` excludes from the deploy (`**/.*`). Everything - pages, `/static/*`,
`/sw.js`, the API - is proxied to Cloud Run. One origin, nothing to keep in sync.

The radar overlay PNGs do *not* pass through here: they are served straight from the GCS bucket via
`OVERLAY_PUBLIC_BASE_URL`, so Hosting's transfer allowance carries only HTML, JS and Leaflet.

## Deploying

    firebase deploy --only hosting

## The site must live in the project that holds the service

Firebase Hosting cannot rewrite to a Cloud Run service in another project. The site and the service
have to be in the same one - here, `rainchecker-195519`.

This is worth its own heading because the mistake is the natural one. "Firebase project" sounds like
the thing you create in order to have a Firebase site, so the obvious move is to make a new project
named after the site you want. That gives you a project whose *default* site is named after the
project ID rather than the name you chose, in a project that cannot see the service. Both wrong, and
neither says so until a deploy serves "Site Not Found".

What is actually wanted is Firebase added to the project that already exists:

    firebase projects:addfirebase rainchecker-195519
    gcloud services enable firebasehosting.googleapis.com --project rainchecker-195519
    firebase hosting:sites:create <site> --project rainchecker-195519

`tests/test_packaging.py::test_the_hosting_proxy_agrees_with_the_service_it_proxies` checks that
`.firebaserc` and `infra/variables.tf` name the same project, so a stray one fails the suite.

## Before the first deploy

`firebase hosting:sites:create rainalerts` has to have succeeded - that call is what claims the name,
and it is also the only reliable availability check. Visiting `https://rainalerts.web.app` is not:
a site someone created and never deployed to serves the same "Site Not Found" page as a name nobody
has taken, so a browser check reads "free" for a name you cannot have.

If the name was taken, change `site` in `firebase.json` and the target in `.firebaserc` together -
they must agree, and `firebase deploy` fails confusingly when they do not.

## After the first deploy

Two things in `infra/` change *together*, and only once `https://<site>.web.app/` is confirmed
serving: `public_base_url`, and `trusted_proxy_hops` from 1 to 2. Hosting is a second proxy, so
without the second change every visitor resolves to the Firebase edge address and they all share one
rate-limit bucket - `subscribe_limit_per_hour` becomes a global cap of five signups an hour.
RUNBOOK.md section 3b has the order and the query that proves the hop count empirically.
