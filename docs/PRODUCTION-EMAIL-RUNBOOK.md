# Fenton4Fitness production email and webhook runbook

## SES production access

Production-access requests and support-case text are operational records, not
source files. Keep request JSON outside Git. The approved use case is
low-volume transactional form confirmation and owner notification only.

Before requesting or appealing production access, confirm the sender identity,
website URL, bounce/complaint process, expected volume, and consent language
are current.

## Google Sheets webhook rotation

In the owning Google account:

1. Open the existing Apps Script project.
2. Create a new web-app deployment rather than editing the exposed deployment.
3. Retain the existing POST payload contract and execute-as-owner setting.
4. Copy the new `/exec` URL without placing it in chat, source control, shell
   history, screenshots, or documentation.
5. Run:

```powershell
.\scripts\rotate-google-sheets-webhook.ps1
```

The helper prompts without echo, preserves all other Lambda environment
variables, validates the URL shape, waits for the Lambda update, and deletes
its restricted temporary file. After end-to-end tests pass, disable the old
Apps Script deployment.

## Required verification after rotation

Submit one clearly labeled test for each category:

- youth athlete
- adult personal training
- team training
- general inquiry
- business website
- testimonial

For every test, verify:

- HTTP 200 and a unique lead ID
- correct DynamoDB `submissionType` and `leadType`
- Google Sheets webhook delivery is `sent`
- owner notification is `sent`
- customer confirmation is `sent`
- no plaintext webhook URL appears in output or logs

Use an address permitted by the SES account while it remains in the sandbox.
After production access is approved, repeat one confirmation test to an
external recipient who has explicitly consented.
