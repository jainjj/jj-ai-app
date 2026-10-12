# jj-ai · Geeko's Cluster Arcade

A fun front end for SUSE AI: roast your cluster, read its fortune, get YAML judged and
more, all answered by the Ollama model running inside the cluster. It's packaged as a
Helm chart that Rancher Fleet deploys to any cluster you label.

The app is a single stdlib-only Python script on the stock `registry.suse.com/bci/python:3.13`
image, so there's no image to build. The chart mounts `files/app.py` and `files/index.html`
from a ConfigMap, and a checksum annotation rolls the pods whenever either file changes.

## Layout

```
jj-ai/                 the Fleet bundle (a Helm chart plus fleet.yaml)
  fleet.yaml           namespace, release name, per-cluster values from labels
  values.yaml          defaults (Ollama URL, model, ingress)
  files/               app.py (backend) and index.html (UI)
  templates/
rancher/gitrepo.yaml   registers this repo with Fleet
```

## Requirements on each target cluster

- An Ollama service. The default is SUSE AI's
  `ollama.simple-chatbot-with-rag-system.svc.cluster.local:11434` with at least one model pulled.
- An ingress controller with a default IngressClass (k3s ships Traefik).

## Deploy with Fleet

1. Push this repo somewhere Fleet can reach, and set `spec.repo` in `rancher/gitrepo.yaml`.
   For a private repo, add a git credential in Rancher and set `spec.clientSecretName`.
2. Register it with Fleet, either in Rancher (**Continuous Delivery → Git Repos → Add Repository**,
   path `jj-ai`) or with:
   ```sh
   kubectl --context rancher apply -f rancher/gitrepo.yaml
   ```
3. Label each cluster that should run it (in Rancher: **Continuous Delivery → Clusters → Edit config → Labels**):

   | Label | Effect |
   |---|---|
   | `jj-ai=enabled` | deploys the app |
   | `jj-ai/host=jj-ai.<ingress-ip>.sslip.io` | sets the Ingress hostname. Without it the Ingress matches any host, so `http://<ingress-ip>/` works |
   | `jj-ai/model=<name>` | preferred Ollama model, e.g. `qwen3`. If Ollama doesn't have it, the app uses the first local model it finds |

Fleet installs it into the `jj-ai` namespace as Helm release `jj-ai`. To change the app,
edit `files/` and push, and Fleet rolls it out to every labelled cluster.

## Settings

Override any of these under `helm.values` in `fleet.yaml`:

| Value | Default |
|---|---|
| `ollama.url` | `http://ollama.simple-chatbot-with-rag-system.svc.cluster.local:11434` |
| `ollama.model` | from the `jj-ai/model` label, else `gpt-oss:latest`; falls back to the first local model Ollama has |
| `maxConcurrent` | `2` simultaneous model calls |
| `ingress.host` / `ingress.className` | from the `jj-ai/host` label / cluster default |

## Without Fleet

```sh
helm upgrade --install jj-ai ./jj-ai -n jj-ai --create-namespace \
  --set ingress.host=jj-ai.<ingress-ip>.sslip.io
```
