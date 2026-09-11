{{/*
Fully qualified app name - standard chart-name/release-name collision guard.
*/}}
{{- define "recon-agent.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Postgres service name, derived the same way so templates/secret.yaml can
build DATABASE_URL without hardcoding it.
*/}}
{{- define "recon-agent.postgresFullname" -}}
{{- printf "%s-postgres" (include "recon-agent.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels. `app: recon-agent` stays fixed (not release-templated) - it's
the exact selector issue #15's verify recipe uses:
`kubectl wait --for=condition=ready pod -l app=recon-agent`.
*/}}
{{- define "recon-agent.labels" -}}
app: recon-agent
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}
