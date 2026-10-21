# KubeKart · a microservice shop on SUSE AI

KubeKart is an online shop for cloud-native gear, built as six small microservices.
It has a paid membership tier (**KubeKart Plus**) and **Kiko**, a shopping assistant that
runs on the SUSE AI Ollama installed in the same cluster.

Like jj-ai, every service is a stdlib-only Python script on the stock
`registry.suse.com/bci/python:3.13` image, mounted from a ConfigMap. There are no images
to build and no registry to set up, so the chart deploys as-is to any cluster.

## Architecture

```
                   Ingress
                      │
              ┌───────▼────────┐   serves the shop UI, routes /api/<service>/*,
              │  web (gateway) │   aggregates /api/status
              └─┬────┬────┬──┬─┘
       ┌────────┘    │    │  └───────────┐
  ┌────▼───┐  ┌──────▼─┐ ┌▼───────┐ ┌────▼──────┐
  │ users  │  │catalog │ │ orders │ │ assistant │──▶ SUSE AI Ollama
  │ +Plus  │  │products│ │checkout│ │ "Kiko"    │
  └───┬────┘  └────────┘ └───┬────┘ └───────────┘
      │      (internal only) │
      └──────▶ payments ◀────┘   demo gateway
```

| Service | Does | State |
|---|---|---|
| `web` | Serves the single-page shop and is the only service behind the Ingress. Proxies `/api/users`, `/api/catalog`, `/api/orders` and `/api/assistant`. | none (2 replicas) |
| `catalog` | Products, categories and Plus member prices (10% off). | none (2 replicas) |
| `users` | Accounts (PBKDF2 password hashes), signed sign-in tokens, joining and cancelling Plus. | SQLite on its own volume |
| `orders` | Checkout and order history. Gets prices from catalog, the buyer's *current* tier from users, and charges payments, so the browser can't change any of them. | SQLite on its own volume |
| `payments` | The **demo** payment gateway. It records a ledger entry and succeeds, or declines if asked to simulate a decline. No real money and no card details, ever. Not routable from outside, and it only accepts calls signed with the internal service key. | SQLite on its own volume |
| `assistant` | Kiko. Streams replies from Ollama, knows the live catalog, and tags products so the UI shows add-to-cart buttons. Free shoppers and guests get 5 chats a day; Plus is unlimited. | in-memory daily counts |

Sign-in tokens and service-to-service calls are signed with a secret the chart generates
once (`<release>-secret`) and keeps across upgrades.

### Tiers

| | Free | Plus (€4.99/month, demo payment) |
|---|---|---|
| Prices | list price | 10% member price |
| Shipping | €4.95, free from €50 | always free |
| Kiko chats | 5 a day | unlimited |

## Requirements on each cluster

- SUSE AI's Ollama (default `ollama.simple-chatbot-with-rag-system.svc.cluster.local:11434`)
  with at least one model. Kiko prefers `gpt-oss:latest` and otherwise uses the first local model.
- A default StorageClass for the three 1 Gi volumes (k3s ships `local-path`).
- An ingress controller with a default IngressClass (k3s ships Traefik).

## Deploy with Fleet

1. Register the bundle on the Rancher management cluster:
   ```sh
   kubectl --context rancher apply -f rancher/kubekart-gitrepo.yaml
   ```
2. Label each cluster that should run it:

   | Label | Effect |
   |---|---|
   | `kubekart=enabled` | deploys the shop into namespace `kubekart` |
   | `kubekart/host=kubekart.<ingress-ip>.sslip.io` | Ingress hostname. Without it, the shop answers on any host |
   | `kubekart/model=<name>` | preferred Ollama model, e.g. `qwen3` |

   To show the platform on the shop page, add the **annotation** (not a label, since it has spaces)
   `kubekart/platform`, e.g. `SUSE Rancher, SUSE RKE2, SUSE AI Factory`. Without it the line is hidden.
   Without Fleet, pass it with `--set`, escaping commas: `--set platform="SUSE Rancher\, SUSE RKE2\, SUSE AI Factory"`.

## Deploy without Fleet

```sh
helm upgrade --install kubekart ./kubekart -n kubekart --create-namespace \
  --set ingress.host=kubekart.<ingress-ip>.sslip.io
```

## Test it

```sh
kubectl -n kubekart port-forward svc/kubekart-web 18080:8080 &
python3 tests/kubekart_smoke.py http://127.0.0.1:18080 --chat
```

The smoke test creates a throwaway account and checks checkout, declines, Plus pricing,
stale-token handling, the assistant quota and that payments isn't publicly reachable.
`--chat` adds one real Kiko reply, which takes 1–3 minutes on a CPU-only cluster.

## Settings (`values.yaml`)

| Value | Default |
|---|---|
| `ollama.url` / `ollama.model` | SUSE AI's Ollama / `gpt-oss:latest` |
| `assistant.freeDailyMessages` | `5` |
| `assistant.maxConcurrent` | `2` simultaneous model calls |
| `ingress.host` / `ingress.className` | any host / cluster default |
| `storage.size` / `storage.storageClass` | `1Gi` / cluster default |
| `services.<name>.replicas` | 2 for web and catalog, 1 for the rest |

## Run it locally

Each service runs with plain `python3` (no dependencies). Ports default to 8080 (web)
through 8085 (assistant), and `DATA_DIR` sets where the SQLite files go:

```sh
cd kubekart/files
for s in web users catalog orders payments assistant; do SERVICE_NAME=$s DATA_DIR=/tmp/kubekart python3 $s.py & done
```
