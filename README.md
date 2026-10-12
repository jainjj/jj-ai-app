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
  values.yaml          defaults (Ollama URL, model, ingress, NeuVector)
  files/               app.py (backend) and index.html (UI)
  templates/
rancher/gitrepo.yaml   registers this repo with Fleet
```

## Requirements on each target cluster

- An Ollama service. The default is SUSE AI's
  `ollama.simple-chatbot-with-rag-system.svc.cluster.local:11434` with model `gpt-oss:latest`.
- An ingress controller with a default IngressClass (k3s ships Traefik).
- Optional: NeuVector, for the DLP policy.

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
   | `jj-ai/neuvector=enabled` | adds the NeuVector policy (below) |

Fleet installs it into the `jj-ai` namespace as Helm release `jj-ai`. To change the app,
edit `files/` and push, and Fleet rolls it out to every labelled cluster.

## NeuVector DLP policy

With `jj-ai/neuvector=enabled`, the chart adds an `NvSecurityRule` that puts the jj-ai group
in **Protect** mode, turns on DLP with the built-in `sensor.creditcard` set to deny, and
whitelists the app's connections (ingress controller → app, kubelet probes, app → Ollama,
DNS, the Kubernetes API) and its single Python process. If a request carrying a card number
is dropped, the UI shows a "Blocked by NeuVector DLP" banner.

Protect blocks *every* connection that isn't whitelisted, so check that the peer names in
`values.yaml` (`neuvector.ingressController`, `neuvector.dns`) match the cluster. Set
`neuvector.policyMode: Monitor` to get DLP alerts without blocking.

## Settings

Override any of these in `fleet.yaml` (globally or in a `targetCustomizations` entry):

| Value | Default |
|---|---|
| `ollama.url` | `http://ollama.simple-chatbot-with-rag-system.svc.cluster.local:11434` |
| `ollama.model` | `gpt-oss:latest` |
| `maxConcurrent` | `2` simultaneous model calls |
| `ingress.host` / `ingress.className` | from the `jj-ai/host` label / cluster default |
| `neuvector.enabled` / `neuvector.policyMode` | from the `jj-ai/neuvector` label / `Protect` |

## Without Fleet

```sh
helm upgrade --install jj-ai ./jj-ai -n jj-ai --create-namespace \
  --set ingress.host=jj-ai.<ingress-ip>.sslip.io
```
