"""The redaction cases shared by the redaction tests and the policy evaluation."""

SENTINELS = [
    "gsk_SENTINEL0123456789abcdef",
    "approver-sentinel-token-7f3a91",
    "postgresql+psycopg://morph:s3ntinelPassw0rd@localhost:5432/morph",
    "s3ntinelPassw0rd",
]

# secret-shaped strings the Redactor was NOT told about: all must be removed
PATTERNS = [
    "gsk_" + "a1B2c3D4e5F6g7H8i9J0",
    "sk-" + "proj1234567890abcdefXYZ",
    "Bearer " + "abcdEFGH12345678.token",
    "bearer " + "xyzXYZ0123456789",
    "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3",
    "xoxb-" + "1234567890-abcdefghij",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "postgresql://user:hunter2pass@db.internal:5432/app",
    "https://admin:p4ssw0rd@example.com/path",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    "Zm9vYmFyMTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaMDEyMzQ1Njc4OQ",
]

# ordinary text that must survive: identifiers, ids, hashes of the kind we print, prose
NEAR_MISSES = [
    "customer_segment_assignment_identifier_v2",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "the quick brown fox jumps over the lazy dog",
    "skeleton_configuration_for_the_support_user_entity",
    "task-5f0d3c1e-uuid-like-but-short",
    "SupportUserToCrmCustomerTransformationStrategy",
    "http://localhost:8102/users",
    "user@example.com wrote about gsk and sk-short",
]
