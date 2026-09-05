# Amazon authorization for a local installation

Amazon Data Core is local software, not a shared Amazon application. The first
release therefore uses **bring your own Amazon application (BYOA)**. The setup
wizard stores credentials only in the user's local `.env` and verifies them
against Amazon before requesting business data.

## Supported path: your own seller organization

1. Use an Amazon Professional selling account. Amazon does not allow Individual
   selling accounts to create private seller applications.
2. Register as an SP-API private developer in Amazon's Solution Provider Portal.
3. Register a private seller application and request only the roles needed by
   the datasets you want:
   - **Inventory and Order Tracking** for orders and inventory;
   - **Finance and Accounting** for settlement reports.
4. In Developer Central, self-authorize that private application for your seller
   account. Amazon generates the LWA refresh token during self-authorization.
5. Open the application's LWA credentials and have these three values ready:
   client ID, client secret and refresh token.
6. Run `./scripts/onboard.sh`. Enter the three secrets personally in the
   temporary localhost page that opens. Do not paste them into an Agent or chat
   window. If no browser is available, use
   `./scripts/onboard.sh --terminal-config` and enter them in a real terminal.

Amazon's official references:

- [SP-API registration overview](https://developer-docs.amazon.com/sp-api/lang-en_EN/docs/sp-api-registration-overview)
- [Register a private developer](https://developer-docs.amazon.com/sp-api/docs/register-as-a-private-developer)
- [Register an application](https://developer-docs.amazon.com/sp-api/docs/registering-your-application)
- [Onboarding and self-authorization](https://developer-docs.amazon.com/sp-api/docs/onboarding-overview)
- [View LWA application credentials](https://developer-docs.amazon.com/sp-api/lang-US/docs/viewing-your-application-information-and-credentials)

If a role is added after a token was created, generate a new authorization so
the token includes that role. A valid token can therefore pass LWA verification
but still receive `403` from a dataset whose role is missing. The first-sync
summary keeps these states separate.

## Optional Amazon Ads

Amazon Ads is a separate application, approval and OAuth authorization from
SP-API. If the user already has Ads API access, the wizard can collect its client
ID, client secret, refresh token and Ads profile ID. Otherwise answer `n`; orders,
FBA inventory and settlements do not depend on Ads credentials.

Amazon documents that Ads API access requires an application and approval:
[Amazon Ads API access](https://advertising.amazon.com/about-api).

## What the open-source installer cannot do

The repository cannot register a developer, accept Amazon agreements, request
roles or manufacture a refresh token on a seller's behalf. It also cannot use one
private application's token for unrelated seller organizations.

To serve other companies' sellers through one application, the operator must
register a public SP-API application, pass Amazon review, list it as required,
host HTTPS OAuth callback endpoints and maintain token storage and revocation.
That hosted public-app OAuth service is intentionally outside this local Core.

## Local secret handling

- `.env` is ignored by Git and written atomically with mode `0600`.
- Secrets are entered in a temporary page bound to `127.0.0.1`; the page shuts
  down after submission, cancellation or timeout.
- The browser form never renders existing secret values. A blank secret field
  keeps its saved value when reconfiguring.
- A non-echoing real-terminal wizard remains available with
  `--terminal-config`.
- Status output reports only whether fields exist; it never returns values.
- The database stores sync facts and lineage, not LWA credentials.
- Re-running the wizard lets the user press Enter to retain a saved secret.
- Delete `.env` to remove the local credentials. This does not revoke Amazon
  authorization; revoke it in Amazon's application/authorization settings too.
