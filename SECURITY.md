# Security Policy

## Scope

Hyundai Battery Insight is a local Home Assistant App. It reads Home Assistant data through the Supervisor/Home Assistant API and exposes its UI through Home Assistant ingress only.

## Reporting a vulnerability

If you discover a security issue, please use GitHub's private security-reporting / Security Advisory feature for this repository when available.

If private reporting is not available, open a GitHub issue containing only enough information to establish contact and the affected area. Do not publish secrets, VINs, registration/licence-plate values, tokens, raw vehicle payloads or exploit details in a public issue.

## Raw-data protection

Version 0.1.8 protects complete raw vehicle payloads stored in SQLite using authenticated encryption derived deterministically from the vehicle VIN and an app-specific derivation context.

This is intentionally **not claimed to be high-security encryption**. Because the VIN and source code may both be available to an attacker, the protection should be understood as:

- at-rest obfuscation of identifiers from casual/plaintext inspection;
- ciphertext integrity/tamper detection; and
- reinstall-safe recoverability.

It should not be treated as a substitute for operating-system access control, Home Assistant authentication, encrypted backups or secure host administration.

## Supported versions

Security fixes, if any, are expected to target the latest published release. No long-term support window is guaranteed.
