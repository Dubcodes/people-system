# Frame Pull Inbox

People Intelligence reads approved Frame Pull results over a private HTTP(S)
bridge. It does not mount Frame Pull storage or access its database.

## Configuration

Set these in the People Intelligence service environment, not browser JavaScript:

```text
FRAME_PULL_BASE_URL=http://private-frame-pull-host:8094
FRAME_PULL_API_TOKEN=the-same-value-as-FRAME_PULL_BRIDGE_TOKEN
```

The base URL can use a LAN address, Tailscale, or another private HTTPS tunnel.
The application does not assume a Windows path or localhost address. If the URL
is not configured, the Inbox shows an unavailable state while normal People
workflows remain available.

## Operator flow

Open `/frame-pull`. The page lists approved grouped results and approved
standalone interviews, supports ready, imported, track, and name filters, and
proxies portraits through People Intelligence. The browser never receives the
Frame Pull token.

An exact existing People name is only suggested. An operator must explicitly
choose an active existing person before import; this integration never creates
a person from OCR or a Frame Pull name.

Import validates and stores a People-owned JPEG and thumbnail using the normal
image storage rules. `frame_pull_imports` stores the stable group ID, selected
person/image IDs, original identity, track/role context, evidence summary, and
acknowledgement state. The group ID is unique, so repeated imports do not make
duplicate records.

The People transaction commits before Frame Pull receives `mark-exported`.
The provenance record explicitly reports `ack_pending`, `ack_failed`, or
`acknowledged`. If acknowledgement is temporarily unavailable, the committed
import remains intact and `POST /api/frame-pull/imports/{id}/retry-acknowledgement`
retries only the downstream acknowledgement; it never imports a second image.

## Development checks

```powershell
cd J:\projects\people-system\app
python -m compileall -q .
python -m pytest -q
```

The automated tests use SQLite fixtures and a fake Frame Pull client. They do
not require the real bridge or a production People database.
