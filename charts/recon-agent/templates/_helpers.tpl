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

{{/*
Dev Postgres password. An explicit postgresql.password wins. Otherwise reuse
the one already stored in this release's Secret, so `helm upgrade` doesn't
rotate it out from under the running database, and only generate a fresh one
on first install. Call it once per render: randAlphaNum differs per call.
*/}}
{{- define "recon-agent.postgresPassword" -}}
{{- if .Values.postgresql.password }}
{{- .Values.postgresql.password }}
{{- else }}
{{- $existing := lookup "v1" "Secret" .Release.Namespace (include "recon-agent.fullname" .) }}
{{- if and $existing $existing.data (hasKey $existing.data "POSTGRES_PASSWORD") }}
{{- index $existing.data "POSTGRES_PASSWORD" | b64dec }}
{{- else }}
{{- randAlphaNum 24 }}
{{- end }}
{{- end }}
{{- end }}
