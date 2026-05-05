{{/*
Expand the chart name.
*/}}
{{- define "nexus-hub.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a fully qualified app name.
*/}}
{{- define "nexus-hub.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label.
*/}}
{{- define "nexus-hub.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "nexus-hub.labels" -}}
helm.sh/chart: {{ include "nexus-hub.chart" . }}
app.kubernetes.io/name: {{ include "nexus-hub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels shared by a specific component.
*/}}
{{- define "nexus-hub.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nexus-hub.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "nexus-hub.nexusName" -}}
{{- printf "%s-nexus" (include "nexus-hub.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nexus-hub.mcpFrontendName" -}}
{{- printf "%s-mcp-frontend" (include "nexus-hub.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nexus-hub.postgresName" -}}
{{- printf "%s-postgres" (include "nexus-hub.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nexus-hub.redisName" -}}
{{- printf "%s-redis" (include "nexus-hub.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nexus-hub.postgresSecretName" -}}
{{- if .Values.postgres.internal.enabled -}}
{{- default (printf "%s-postgres" (include "nexus-hub.fullname" .)) .Values.postgres.auth.existingSecret | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- default (printf "%s-postgres-external" (include "nexus-hub.fullname" .)) .Values.postgres.external.existingSecret | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "nexus-hub.postgresPasswordKey" -}}
{{- if .Values.postgres.internal.enabled -}}
{{- .Values.postgres.auth.existingSecretPasswordKey -}}
{{- else -}}
{{- .Values.postgres.external.existingSecretPasswordKey -}}
{{- end -}}
{{- end -}}

{{- define "nexus-hub.postgresHost" -}}
{{- if .Values.postgres.internal.enabled -}}
{{- include "nexus-hub.postgresName" . -}}
{{- else -}}
{{- required "postgres.external.host is required when postgres.internal.enabled=false" .Values.postgres.external.host -}}
{{- end -}}
{{- end -}}

{{- define "nexus-hub.postgresPort" -}}
{{- if .Values.postgres.internal.enabled -}}
5432
{{- else -}}
{{- .Values.postgres.external.port -}}
{{- end -}}
{{- end -}}

{{- define "nexus-hub.postgresDatabase" -}}
{{- if .Values.postgres.internal.enabled -}}
{{- .Values.postgres.auth.database -}}
{{- else -}}
{{- .Values.postgres.external.database -}}
{{- end -}}
{{- end -}}

{{- define "nexus-hub.postgresUsername" -}}
{{- if .Values.postgres.internal.enabled -}}
{{- .Values.postgres.auth.username -}}
{{- else -}}
{{- .Values.postgres.external.username -}}
{{- end -}}
{{- end -}}

{{- define "nexus-hub.redisUrl" -}}
{{- if .Values.redis.internal.enabled -}}
{{- printf "redis://%s:6379" (include "nexus-hub.redisName" .) -}}
{{- else -}}
{{- required "redis.external.url is required when redis.internal.enabled=false and redis.external.existingSecret is not set" .Values.redis.external.url -}}
{{- end -}}
{{- end -}}
