{{- define "kubebank.labels" -}}
app.kubernetes.io/part-of: kubebank
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
