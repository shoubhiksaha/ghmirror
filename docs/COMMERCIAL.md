# Commercial deployment and support offering (DRAFT)

> **Status: draft for the owner's decisions.** Everything marked **[DECIDE]** is a business choice:
> price, legal entity, licence, response times. The rest describes what the product and its
> documentation already support today, and the work still needed before selling it (§8).

## 1. What is being sold

**The problem:** an organization whose code, reviews and history live only on GitHub has a single
point of failure. GitHub can be unavailable, the account can be suspended, an admin account can be
compromised, or a mass deletion can happen. ghmirror keeps a faithful, continuously updated copy
in GitLab that the customer runs themselves (cloud and on-premises). The copy has the same
numbers, authors and dates, and it never deletes. A tested failover takes minutes.

**Who it is for:**
- **Regulated industries**, which must show an exit plan and a recoverable copy of source code
  (operational-resilience, DORA-style continuity requirements).
- **Organizations with data-sovereignty needs**, which want a copy on their own infrastructure.
- **Engineering organizations of roughly 50–2,000 developers** that depend on GitHub and want
  insurance without migrating away.

**Why not an alternative:**
- **GitLab's pull mirroring** copies git only: no issues, no pull requests, no reviews.
- **GitHub's migration archives** are a manual export, with no continuous sync and no failover.
- **One-off importers** run once and do not keep the copy current.

Details: `RESEARCH-prior-art.md`.

## 2. Packaging (decided: open source, AGPL-3.0-or-later, dual-licensed)

ghmirror is open source under the **GNU AGPL v3.0 or later** (`LICENSE`). Revenue comes from:

| Offer | What the customer gets | Who buys it |
|---|---|---|
| Deployment service (§3) | A verified copy and a successful failover drill | Teams without time to set it up |
| Support subscription (§4) | Updates, SLA, help during a real failover | Anyone running it in production |
| Commercial licence | The same code without the AGPL obligations | Companies that modify it and do not want to publish their changes, or that embed it in a product or hosted service |

**Why AGPL:** anyone may use and audit the code, which buyers of a recovery tool want (the side
channel is root-equivalent, SECURITY.md §4.3). Anyone who distributes a modified version, or offers
one to users over a network, must publish the changes under the AGPL. A competitor cannot take the
code into a closed product or hosted service. Companies that need that pay for the commercial
licence.

## 3. Deployment service

A fixed-scope engagement that ends with a verified copy and a successful failover drill.

| Phase | Activities | Deliverable | Typical effort |
|---|---|---|---|
| 0. Assessment | Inventory of the GitHub estate (repositories, size, LFS, issues/PRs, members); API budget; identity source (SSO/IdP); GitLab hosting choice | Sizing per CAPACITY.md, network plan, and a go/no-go on the support matrix | 2–3 days |
| 1. GitLab hosts | Provision or review two self-managed GitLabs (cloud and on-prem), backups, TLS, SSO | Two GitLabs passing `ghmirror check` prerequisites | 2–5 days |
| 2. Engine install | INSTALL.md, the hardening checklist (SECURITY.md §5), webhook endpoint, alert channel | Running engine, `check` all `ok` | 1 day |
| 3. First copy | Pilot group, then the full estate; identity map (`identities.yaml`) with HR/IT | Parity report **PASS** for the whole estate (sample: `docs/samples/`) | 2–5 days elapsed, mostly unattended |
| 4. Drill and handover | Failover drill with the customer's team (promote, developer workflow, demote); runbook walkthrough | Drill report with measured RTO/RPO; trained operators | 1 day |

**Effort to price:** about 8–15 consultant days per installation. **[DECIDE]** daily rate or a
fixed price per size band.

**Customer responsibilities:** hosting and operating the GitLab servers and the engine VM; an
organization owner to install the GitHub App; SSO configuration; deciding when to fail over.

## 4. Support subscription **[DECIDE: tiers, hours, prices]**

| | Standard | Business | Critical |
|---|---|---|---|
| Updates and security fixes | ✓ | ✓ | ✓ |
| Support hours | business hours, one time zone | business hours, two time zones | 24×7 for Severity 1 |
| Channels | email / portal | + scheduled calls | + phone / bridge for Severity 1 |
| Assisted failover drill | — | 1 per year | 2 per year |
| Help during a real failover | best effort | business hours | 24×7, named engineer |
| GitLab upgrade compatibility check | release notes | ✓ | ✓, before the customer upgrades |
| Price basis | per engine installation per year, banded by number of mirrored repositories **[DECIDE]** | | |

### Severity definitions and target response times (proposal)

| Severity | Definition | Standard | Business | Critical |
|---|---|---|---|---|
| 1: Critical | Failover needed and `promote` fails, or the mirror is halted and GitHub is unavailable | 1 business day | 4 business hours | 1 hour, 24×7 |
| 2: High | Mirroring stopped, or the deletion guard halted with no clear cause; parity drift that `--repair` does not clear | 2 business days | 1 business day | 4 hours |
| 3: Normal | Degraded sync, a single repository failing, a documentation question | 3 business days | 2 business days | 1 business day |
| 4: Low | Feature requests, cosmetic issues | best effort | best effort | best effort |

These are response targets, not resolution guarantees. The product is designed so that a
Severity 1 situation is rare: the copies remain readable even when the engine is down. A failed
promote is reversible with `demote`, and the drill exists to find such problems in advance.

### What support needs from the customer (diagnostics bundle)

- `ghmirror check` and `ghmirror status` output
- the last parity report
- `docker compose logs --since 24h engine`
- the ghmirror version (`git rev-parse HEAD`)
- the GitLab version

No tokens are needed. Never send `.env` or `secrets/`.

## 5. Release and compatibility policy (proposal)

- **Versioning:** semantic versioning with tagged releases. Support covers the current minor
  release and the one before it.
- **GitLab compatibility:** GitLab ships a minor release every month. Each one is run through the
  e2e suite (`e2e/run_e2e.sh`) within 10 working days, and the support matrix is updated.
  Customers are told which GitLab versions are verified before they upgrade.
- **GitHub API changes:** watch GitHub's API changelog and deprecations; fix within the deprecation
  window.
- **Security fixes:** coordinated disclosure through the process in SECURITY.md §7. Fixes are
  backported to both supported minor releases.

## 6. Commercial terms checklist **[DECIDE]**

- Legal entity, contracting party, governing law
- Pricing:
  - per installation, per repository band, or per developer
  - service day rate
  - currency and payment terms
- Licence and EULA (§7); support terms with the SLA from §4; data-processing terms (the engine
  processes source code and personal data such as names and emails, see SECURITY.md §4.9)
- Liability cap. The tool deliberately never deletes, but customers will ask about data-loss
  liability.
- Trial: for example a 30-day pilot on up to 20 repositories, with the deployment service credited
  on purchase.

## 7. Legal and naming items **[DECIDE]**

| Item | Status | Needed |
|---|---|---|
| Licence | **AGPL-3.0-or-later** (`LICENSE`) | Write the commercial licence text (§2). Add third-party notices for the dependencies (`engine/requirements.lock`: requests, PyYAML, PyJWT, cryptography, psycopg, …), all AGPL-compatible |
| Product name | "ghmirror" has not been checked | Trademark and name search before marketing; consider a distinct brand |
| GitHub and GitLab names | Used to describe compatibility | Use them only nominatively ("works with GitHub"), never as part of the product name or logo |
| GitHub terms | ghmirror reads the customer's own data with the customer's credentials, within API limits | Nothing further; mention in the EULA that the customer authorizes the access |
| Contributor rights | Single author so far | **Required for dual licensing:** a contributor licence agreement before merging any outside contribution, so the commercial licence (§2) can cover all of the code |

## 8. Readiness gaps before the first paying customer

Ordered by risk, these are the things a careful buyer will ask about.

| # | Gap | Why it matters | Effort (estimate) |
|---|---|---|---|
| 1 | **No end-to-end test with a GitHub organization** (teams, members, the App manifest flow with live clicks) | The main target customer is an organization, and the tests used a personal account | 2–3 days with a test organization |
| 2 | **No load test** on a realistic estate (hundreds of repositories, tens of thousands of issues). One large history is tested (`git/git`, 86,000 commits, CAPACITY.md §3.1) | Metadata throughput is extrapolated from one small repository | 1 week, including fixes |
| 3 | **No CI pipeline** (unit tests, image build, e2e against new GitLab releases) | Needed for the compatibility promise in §5 | 2–3 days |
| 4 | **No tagged releases, no published image** | Customers need a version to pin and an image they can verify | 1 day; sign images (e.g. cosign) |
| 5 | **No versioned schema migrations** | Upgrades that change the state schema need a migration path | 1–2 days for a migration framework |
| 6 | **No external security review** | The side channel and admin tokens make this a high-value target (SECURITY.md) | external engagement |
| 7 | **No commercial licence text yet** (the open-source licence is AGPL) | Needed before selling the commercial licence (§2) | legal |
| 8 | **Single maintainer** | Support tiers need on-call cover | hiring or partner |
| 9 | **GitLab-to-GitLab metadata sync after failover is not built** | After failover the secondary gets git only (SUPPORT-MATRIX §4) | 2–3 weeks |
| 10 | **No admin UI**; operations are CLI plus alerts | Fine for platform teams, a hurdle for others | optional |

**Already in place:**
- the one-command install (INSTALL.md)
- pinned builds
- the security model (SECURITY.md)
- the support matrix
- capacity and recovery figures (CAPACITY.md)
- sample parity reports
- a recorded failover drill (`docs/demo/`)
- a runbook, and a log of 73 risks checked, including 31 defects found in testing and fixed (ROADBLOCKS.md)
