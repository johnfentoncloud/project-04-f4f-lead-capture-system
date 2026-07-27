# Lambda deployment source

This directory contains the credential-free source for the existing
`f4f-lead-handler` Lambda function.

Required environment variables:

- `DYNAMODB_TABLE`
- `GOOGLE_SHEETS_WEBHOOK_URL`
- `OWNER_EMAIL`
- `SES_FROM_EMAIL`
- `ALLOWED_ORIGINS`

The Google webhook URL and email configuration must be supplied through Lambda
configuration or AWS-managed secret storage. Never commit their values.

The function preserves the original lead keys while storing normalized fields
for fitness, website-service, and testimonial submissions. A testimonial is
stored for private review only; this function contains no publication path.
