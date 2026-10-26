# KubeBank · a microservice demo bank on SUSE AI

KubeBank is a demo bank built from five small microservices. It has accounts,
payments between customers, savings goals, a virtual card and real-time fraud checks. Its
AI money coach, **Penny**, runs on the SUSE AI Ollama in the same cluster, so customer data
never leaves it.

**Every account and euro is play money.** The bank never asks for card numbers, real bank
details or anything else that would make it a real bank.

Every service is a stdlib-only Python script on the stock `registry.suse.com/bci/python:3.13`
image, mounted from a ConfigMap. There are no images to build, so the chart deploys as-is to
any cluster.

## Install

```sh
helm install kubebank oci://ghcr.io/jainjj/charts/kubebank -n kubebank --create-namespace \
  --set ingress.host=kubebank.<ingress-ip>.sslip.io
```

Then open the host you set, or skip the Ingress and use
`kubectl -n kubebank port-forward svc/kubebank-web 8080:8080`. Check it with
`helm test kubebank -n kubebank`.

Common variations:

```sh
# Your own Ollama (any namespace or host) and model
--set ollama.url=http://ollama.ollama.svc:11434 --set ollama.model=qwen2.5:3b
# A specific ingress class, with TLS from cert-manager
--set ingress.className=nginx \
--set ingress.annotations."cert-manager\.io/cluster-issuer"=letsencrypt \
--set 'ingress.tls[0].secretName=kubebank-tls' --set 'ingress.tls[0].hosts[0]=bank.example.com'
# No Ingress at all
--set ingress.enabled=false
```

Uninstalling keeps the generated signing secret (`kubebank-secret`), so a reinstall still accepts
existing sign-ins. Delete the namespace to remove everything, including the data volumes.

## Architecture

```
                 Ingress
                    │
            ┌───────▼────────┐   serves the bank UI, routes /api/<service>/*,
            │  web (gateway) │   aggregates /api/status
            └──┬─────┬─────┬─┘
       ┌───────┘     │     └────────┐
  ┌────▼───┐   ┌─────▼────┐   ┌─────▼─────┐
  │ users  │◀──│ accounts │◀──│  advisor  │──▶ SUSE AI Ollama
  │sign-in │   │  ledger  │   │ "Penny"   │
  └────────┘   └─────┬────┘   └───────────┘
       (internal only)│
                ┌─────▼────┐
                │  fraud   │  rule-based risk scoring, milliseconds
                └──────────┘
```

| Service | Does | State |
|---|---|---|
| `web` | Serves the single-page bank and is the only service behind the Ingress. Proxies `/api/users`, `/api/accounts` and `/api/advisor`. | none (2 replicas) |
| `users` | Customers (PBKDF2 password hashes), signed sign-in tokens, and email lookup so customers can pay each other. | SQLite on its own volume |
| `accounts` | The ledger: accounts, balances, transactions, transfers, goals, the virtual card and spending insights. Money moves in a single SQLite transaction and can't overdraw. A new customer's accounts are seeded with 60 days of believable activity. | SQLite on its own volume |
| `fraud` | Scores every payment to another customer and every card purchase: share of balance, size against your usual spending, new payee, payment bursts, scam words ("gift card", "crypto", "urgent"…), overseas merchants and the time of night. Risky payments are **held** until the customer confirms them, and risky card purchases are **declined**. Not routable from outside. | none (2 replicas) |
| `advisor` | Penny. Streams replies from Ollama, grounded in the customer's own accounts, which the accounts service hands over per request. 25 questions per customer per day. | in-memory daily counts |

Sign-in tokens and service-to-service calls are signed with a secret the chart generates
once (`<release>-secret`) and keeps across upgrades. The internal APIs (`fraud`, the users
email lookup and the accounts summary) only accept calls carrying a key derived from that secret.

### What Penny (SUSE AI) does

| Where | Feature |
|---|---|
| ✨ Ask Penny (chat) | Talk about your money: "Can I afford a €900 laptop?", "Where could I cut back?" |
| Home / Insights → **Explain my month** | A spending review against last month, with concrete ways to save |
| ✨ on any transaction | What the payment most likely is, whether it recurs, and how it compares to usual |
| Goals → **Plan it** | A monthly savings plan and the spending cuts that would fund it |
| Held payment → **Why?** | Why fraud checks held it, what scam it resembles, and questions to ask yourself before confirming |

The fraud service is rule-based on purpose. It sits on the payment path and must answer in
milliseconds, and a CPU-only model takes minutes. The LLM is used afterwards to *explain* a hold.

### Try the demo

- **Move money** between your own accounts, or **Pay someone** by their KubeBank email
  (register a second customer to try it).
- A payment with a reference like "urgent gift card" or a big share of your balance gets held.
  Ask Penny **Why?**, then confirm or cancel it.
- On the card, **Freeze** it, then **Simulate tap-to-pay** (declined). **Simulate a dodgy
  purchase** shows fraud checks declining an overseas crypto/gift-card merchant.

## Requirements

- Kubernetes 1.25+.
- An Ollama API for Penny. The default is SUSE AI's
  (`ollama.simple-chatbot-with-rag-system.svc.cluster.local:11434`); set `ollama.url` for any other.
  Penny prefers `gpt-oss:latest` and otherwise uses the first local model. Without Ollama the
  bank works and only Penny's answers fail.
- A default StorageClass for the two 1 Gi volumes, or set `storage.storageClass`.
- An ingress controller, unless `ingress.enabled=false`.

## Deploy with Fleet

1. Register the bundle on the Rancher management cluster:
   ```sh
   kubectl --context rancher apply -f rancher/kubebank-gitrepo.yaml
   ```
2. Label each cluster that should run it:

   | Label | Effect |
   |---|---|
   | `kubebank=enabled` | deploys the bank into namespace `kubebank` |
   | `kubebank/host=kubebank.<ingress-ip>.sslip.io` | Ingress hostname. Without it, the bank answers on any host |
   | `kubebank/model=<name>` | preferred Ollama model, e.g. `qwen3` |

   To show the platform in the page footer, add the **annotation** `kubebank/platform`
   (an annotation, since it holds spaces), e.g. `SUSE Rancher, SUSE RKE2, SUSE AI`.

`fleet.yaml` sets `takeOwnership: true`, so Fleet can adopt a release first installed by hand.

## Install from a checkout

```sh
helm upgrade --install kubebank ./kubebank -n kubebank --create-namespace \
  --set ingress.host=kubebank.<ingress-ip>.sslip.io
```

## Test it

```sh
kubectl -n kubebank port-forward svc/kubebank-web 18180:8080 &
python3 tests/kubebank_smoke.py http://127.0.0.1:18180 --ai
```

The smoke test creates two throwaway customers and checks the seeded ledger (every balance
equals the sum of its transactions), transfers, overdraft and double-confirm protection, a
fraud hold, card freeze and fraud declines, goals, and that the fraud service and internal
APIs aren't publicly reachable. `--ai` adds one real Penny reply, which takes 1–3 minutes on
a CPU-only cluster.

## Settings (`values.yaml`)

| Value | Default | |
|---|---|---|
| `ollama.url` / `ollama.model` | SUSE AI's Ollama / `gpt-oss:latest` | Penny's model endpoint |
| `advisor.dailyQuestions` | `25` | per customer per day |
| `advisor.maxConcurrent` | `2` | simultaneous model calls |
| `ingress.enabled` | `true` | |
| `ingress.host` | empty (any host) | |
| `ingress.className` | empty (cluster default) | |
| `ingress.annotations` / `ingress.tls` | none | e.g. cert-manager |
| `existingSecret` | empty (generated) | a Secret with key `bank-secret`; use it with Argo CD or `helm template`, which can't keep a generated one |
| `storage.size` / `storage.storageClass` | `1Gi` / cluster default | |
| `resources` | 20m/48Mi requests, 500m/192Mi limits | per container |
| `nodeSelector` / `tolerations` / `affinity` | none | |
| `services.<name>.replicas` | 2 for web and fraud, 1 for the rest | users and accounts must stay at 1 (SQLite) |
| `image` / `imagePullPolicy` | `registry.suse.com/bci/python:3.13` / `IfNotPresent` | |
| `platform` | empty | footer text "Running on …" |

## Run it locally

Each service runs with plain `python3` (no dependencies). Ports default to 8080 (web)
through 8084 (advisor), and `DATA_DIR` sets where the SQLite files go. Point `OLLAMA_URL`
at any Ollama:

```sh
cd kubebank/files
for s in web users accounts fraud advisor; do SERVICE_NAME=$s DATA_DIR=/tmp/kubebank OLLAMA_URL=http://localhost:11434 python3 $s.py & done
```

## Publishing a new chart version

1. Bump `version` in `Chart.yaml` (and `appVersion` if the app changed).
2. Push a matching tag, e.g. `git tag kubebank-v0.2.0 && git push origin kubebank-v0.2.0`.

The `kubebank-chart` GitHub Actions workflow lints the chart, pushes it to
`oci://ghcr.io/<owner>/charts/kubebank` and attaches the `.tgz` to a GitHub release. The first
time, make the `charts/kubebank` package public in GitHub (**Packages → kubebank → Package
settings → Change visibility**) so people can pull it without signing in.
