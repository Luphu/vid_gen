{{- define "srvmon.labels" -}}
app.kubernetes.io/part-of: srvmon
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "srvmon.pin" -}}
nodeSelector:
  kubernetes.io/hostname: {{ .Values.nodeName }}
{{- end }}

{{- define "srvmon.pullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}
