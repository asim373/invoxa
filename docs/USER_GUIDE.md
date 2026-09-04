# Invoxa user guide

## Accounts and sign-in

Use **Create account** with an email and password of at least 12 characters. New registrations
receive the reviewer role. Existing users can sign in with email/password. **Continue with
Google** appears when OAuth is configured; a verified identity links to a matching normalized
email or creates a reviewer account, then uses Google's durable subject on later sign-ins.

Use **Forgot password?** for a short-lived reset link. The response does not disclose whether an
account exists. Resetting increments `auth_version`, invalidating previously issued tokens.

## Upload and processing

Upload PDF, JPG/JPEG, or PNG from Documents. Invoxa validates type, extension, binary signature,
size, PDF page count, and image pixel count, stores an internally generated filename, and queues
processing.

```text
Queued -> Processing -> Completed
                    \-> Failed
```

The worker extracts embedded PDF text first and uses OCR when needed; images use OCR. Completed
documents expose text, fields, line items, validation, confidence/provenance, and source preview.
A failed document keeps a safe error and can be reprocessed by an authorized role.

Startup recovery restores abandoned in-flight jobs, and periodic reconciliation republishes
queued database records missing a Redis job. AI Analysis failure does not erase extraction.

## Documents and review

Search without a separate Search button; filter by status/file type, sort, and paginate. Available
actions include viewing, export, reprocess, and delete according to role. Compare invoice number,
vendor/customer, dates, currency, subtotal, tax, total, and line items with the source viewer.
Confidence indicates review priority, not guaranteed correctness.

## AI Analysis

After extraction, AI Analysis can identify arithmetic/line-item inconsistencies, exact and
probable duplicates, statistically unusual same-currency amounts, vendor deviations, missing
fields, low confidence, conservative date anomalies, and tax/total consistency issues.

Every actionable finding explains why it exists and provides observed/expected evidence where
available. Severity is low, medium, high, or sparingly critical. Invoxa says unusual,
inconsistent, or possible duplicate; it does not accuse fraud.

Reviewers/admins can acknowledge, resolve, and reopen findings without deleting traceability.
Use document links to compare evidence with the source.

## Analytics, reports, and exports

Overview metrics and trends are server-calculated and ownership-scoped. They cover documents,
invoices, review states, findings, validation, spend, vendors, and status distributions. Currency
totals stay separate; no FX conversion is implied. Use preset/custom dates and available vendor,
status, and severity filters.

Reports include:

- **Financial summary:** counts, currency-grouped spend/tax/averages, period, and top vendors
- **AI Analysis:** severity, status, categories, affected documents, unresolved findings
- **Processing quality:** processing states, extraction/validation issues, confidence review data

Export document or report data as CSV, XLSX, or JSON. Server filenames are safe, access is
owner-scoped, and spreadsheet text beginning with formula-trigger characters is neutralized.

## Roles and isolation

| Role | Access |
| --- | --- |
| Admin | Read and mutate owned workflow data |
| Reviewer | Read and mutate owned workflow data; default new-account role |
| Viewer | Read owned data; cannot upload, reprocess, delete, or change finding state |

Every document, file, finding, analytics, report, and export query is owner-scoped. Cross-user
resource requests return a non-disclosing not-found response.

## Errors and recovery

- Reprocess failed documents after checking their safe error and source format.
- If analysis fails, extraction remains usable and analysis can be recomputed.
- Sign in again when authentication expires; password reset invalidates older tokens.
- Adjust filters when a report has no matching records; Invoxa does not invent report data.
- Contact the operator for readiness, SMTP, OAuth, storage, or worker infrastructure failures.
