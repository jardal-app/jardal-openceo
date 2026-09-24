# OpenCEO Architecture Direction

Version: 1.1 (2026-09-23)

## Purpose

OpenCEO is an Agent Control Plane. It coordinates AI coding agents,
execution workflows, workers, sessions, quotas and automation.

OpenCEO is not intended to replace Jira, Notion, ERPNext, documentation
platforms or code intelligence systems.

## Architecture Domains

  -----------------------------------------------------------------------
  System                              Responsibility
  ----------------------------------- -----------------------------------
  ERPNext                             Business work management: projects,
                                      tickets, requests, approvals

  Obsidian                            Engineering knowledge memory, a
                                      Markdown vault read and written as
                                      files

  qmd                                 Keyword search over the vault

  Code search and LSP                 Code relationships, read from the
                                      repository when asked

  Git                                 Source code and project
                                      configuration

  OpenCEO                             Agent orchestration and execution
                                      control

  MongoDB                             Runtime execution state
  -----------------------------------------------------------------------

## OpenCEO Responsibilities

OpenCEO manages:

-   Agent execution
-   Providers
-   Workers
-   Sessions
-   Queues
-   Scheduling
-   Quotas
-   Workflow execution
-   Technical approvals

OpenCEO does not own:

-   Complete documentation
-   Engineering memory
-   Code graphs
-   Business information

## ERPNext Role

ERPNext is the business system of record.

It owns:

-   Projects
-   Tasks
-   Tickets
-   Priorities
-   Human approvals
-   Users
-   Business workflows

ERPNext answers:

"What work exists?"

OpenCEO answers:

"How is this work executed?"

## Engineering Knowledge

Obsidian stores:

-   Architecture
-   ADRs
-   Technical decisions
-   Research
-   Incidents
-   Lessons learned

Agents read and write the vault directly as files, with no MCP, following
the `obsidian-vault-links` skill, and find notes with `qmd search`. The
setup is described in
`/home/develop/development/Jardal/jardal-knowledge/ENGINEERING_MEMORY_SETUP.md`.

## Code Intelligence

Code relationships are read from the code when the question is asked:

-   Targeted search over the repository
-   LSP, where the agent supports it

Nothing about code structure is copied into the vault or stored by
OpenCEO. Graphify is installed on the server but registered in no agent:
its edges are matched by name, it is refreshed by hand, and a stale graph
gives no warning.

The repository answers:

"What is connected?"

Obsidian answers:

"Why was it designed this way?"

## Agent Connectivity

Agents connect directly to:

-   The engineering vault, as files
-   qmd, as a command
-   OpenCEO MCP

There is no Obsidian MCP and no Graphify MCP. OpenCEO is not a mandatory
proxy for every read operation.

## Read vs Write Policy

Knowledge writes:

-   Technical notes
-   Research
-   Documentation
-   Debugging discoveries

can be written directly to engineering memory, following the vault skill
and passing its checker.

Operational writes:

-   Ticket creation
-   Status changes
-   Workflow execution
-   Approvals

must pass through OpenCEO.

## MCP Strategy

Expose capabilities, not raw APIs.

Preferred:

-   ticket.create()
-   ticket.transition()
-   execution.start()
-   execution.status()
-   approval.request()

Avoid exposing complete infrastructure APIs.

## Project Model

A project is distributed:

ERPNext: - identity - tickets - status

Git: - code - configuration

Obsidian: - architecture - decisions

OpenCEO: - execution state

Shared identifier:

project_key

## Final Principle

OpenCEO coordinates intelligence.

ERPNext coordinates work.

Obsidian preserves engineering memory.

Git preserves implementation.
