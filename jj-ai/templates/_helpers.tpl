{{- define "jj-ai.labels" -}}
app.kubernetes.io/name: jj-ai
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
