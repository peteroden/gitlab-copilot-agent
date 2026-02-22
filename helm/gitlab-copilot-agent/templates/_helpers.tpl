{{- define "app.fullname" -}}{{ .Release.Name }}-{{ .Chart.Name }}{{- end }}
{{- define "app.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- define "app.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
{{- define "app.redisPassword" -}}
{{- if .Values.redis.password }}
{{- .Values.redis.password }}
{{- else }}
{{- $secret := lookup "v1" "Secret" .Release.Namespace (include "app.fullname" .) }}
{{- if and $secret $secret.data }}
{{- index $secret.data "REDIS_PASSWORD" | b64dec }}
{{- else }}
{{- randAlphaNum 32 }}
{{- end }}
{{- end }}
{{- end }}
{{- define "app.redisUrl" -}}redis://:{{ include "app.redisPassword" . | urlquery }}@{{ include "app.fullname" . }}-redis:{{ .Values.redis.port }}{{- end }}
{{- define "app.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}{{ default (include "app.fullname" .) .Values.serviceAccount.name }}{{- else }}{{ default "default" .Values.serviceAccount.name }}{{- end }}
{{- end }}
{{- define "app.jobImage" -}}
{{- if .Values.jobRunner.image }}{{ .Values.jobRunner.image }}{{- else }}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end }}
{{- end }}
