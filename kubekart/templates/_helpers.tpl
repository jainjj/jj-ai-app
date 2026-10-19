{{- define "kubekart.labels" -}}
app.kubernetes.io/part-of: kubekart
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
