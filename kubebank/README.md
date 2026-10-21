# KubeBank · a microservice demo bank on SUSE AI

KubeBank is a demo bank built from five small microservices. It has accounts,
payments between customers, savings goals, a virtual card and real-time fraud checks. Its
AI money coach, **Penny**, runs on the SUSE AI Ollama in the same cluster, so customer data
never leaves it.

**Every account and euro is play money.** The bank never asks for card numbers, real bank
details or anything else that would make it a real bank.

Like the other apps in this repo, every service is a stdlib-only Python script on the stock
`registry.suse.com/bci/python:3.13` image, mounted from a ConfigMap. There are no images to
build, so the chart deploys as-is to any cluster.

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

## Requirements on each cluster

- SUSE AI's Ollama (default `ollama.simple-chatbot-with-rag-system.svc.cluster.local:11434`)
  with at least one model. Penny prefers `gpt-oss:latest` and otherwise uses the first local model.
- A default StorageClass for the two 1 Gi volumes (k3s ships `local-path`).
- An ingress controller with a default IngressClass (k3s ships Traefik).

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

## Deploy without Fleet

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

| Value | Default |
|---|---|
| `ollama.url` / `ollama.model` | SUSE AI's Ollama / `gpt-oss:latest` |
| `advisor.dailyQuestions` | `25` per customer |
| `advisor.maxConcurrent` | `2` simultaneous model calls |
| `ingress.host` / `ingress.className` | any host / cluster default |
| `storage.size` / `storage.storageClass` | `1Gi` / cluster default |
| `services.<name>.replicas` | 2 for web and fraud, 1 for the rest |

## Run it locally

Each service runs with plain `python3` (no dependencies). Ports default to 8080 (web)
through 8084 (advisor), and `DATA_DIR` sets where the SQLite files go. Point `OLLAMA_URL`
at any Ollama:

```sh
cd kubebank/files
for s in web users accounts fraud advisor; do SERVICE_NAME=$s DATA_DIR=/tmp/kubebank OLLAMA_URL=http://localhost:11434 python3 $s.py & done
```
