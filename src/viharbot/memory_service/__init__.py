"""Governed memory service (ADR-009, issue #40, PR 1): agent-private and session memory only.

A FastAPI service over plain JSON REST in front of one PostgreSQL database. It is the only holder
of database credentials. Callers are authenticated with the ADR-006 A2 verifier, the caller's agent
identity is derived from the verified ``sub`` only, and a repository-configured policy table
decides what each agent may do. Nothing in the application calls this service yet.
"""
