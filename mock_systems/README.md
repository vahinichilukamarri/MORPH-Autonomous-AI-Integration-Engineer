# Mock enterprise systems

Two independently built systems that MORPH must integrate. They describe the same real-world
person with deliberately different conventions. The mismatches are intentional: they are what
the integration (and the bench answer key in `bench/`) must resolve.

| | Mock CRM (`crm/`, port 8101) | Mock Support (`support/`, port 8102) |
|---|---|---|
| Entity | `Customer` | `User` |
| Auth | `X-API-Key` header | `Authorization: Bearer <token>` |
| Naming | snake_case | camelCase (with one stray `email_address`) |
| Errors | FastAPI default (`{"detail": ...}`) | `{"error": {"code", "message", "details": [{location, field, expected, received}]}}` |
| Validation | lenient defaults | strict: no type coercion, unknown fields rejected |

## Intentional field mismatches (CRM Customer -> Support User)

| Concern | CRM | Support | Rule |
|---|---|---|---|
| Identifier | `customer_id` = `"C-1837"` | `userId` = `1837` (int) | numeric part of the CRM id |
| Source reference | (the id itself) | `externalRef` = `"C-1837"` | keep the original CRM id |
| Name | `first_name` + nullable `last_name` | `fullName` | `"first last"`, trimmed, single space; first name only when last is null |
| Email | `email` | `email_address` | rename |
| Phone | `phone` = `"+919876543210"` (E.164) or null | `phoneNumber` = `"919876543210"` or null | drop the leading `+` |
| Status vocabulary | `status`: ACTIVE / INACTIVE / SUSPENDED | `accountState`: ENABLED / DISABLED / BLOCKED | ACTIVE->ENABLED, INACTIVE->DISABLED, SUSPENDED->BLOCKED |
| Timestamp | `created_at` ISO-8601 UTC string | `createdAt` integer unix seconds | parse and convert |
| Segment vs tier | `segment`: SMB / MIDMARKET / ENTERPRISE | `tier`: STANDARD / PRIORITY | SMB, MIDMARKET -> STANDARD; ENTERPRISE -> PRIORITY |

## Support behaviour worth knowing

- `POST /users` returns 409 when `userId` already exists; `PUT /users/{userId}` is an upsert
  (201 when created, 200 when replaced) and rejects a body whose `userId` differs from the path.
- Validation failures return 422 with the offending field, what was expected and what was
  received.

## Running

```powershell
docker compose up -d --build --wait mock-crm mock-support
```

Defaults for the credentials live in `.env.example` (`CRM_API_KEY`, `SUPPORT_TOKEN`,
`ADMIN_TOKEN`).
