# Projects

Cada proyecto debe definir:
- `projects/<name>/project.yaml`
- `projects/<name>/CONTEXT.md`

## Ejemplo mínimo de `project.yaml`

```yaml
project:
  name: my-project
  repos:
    - name: app
      local_path: /abs/path/repo
      branch_base: main

dev:
  coding_tier: auto
  default_provider: auto
  branch_pattern: "{session_id}-{slug}"
  providers:
    codex:
      command: ["/usr/local/bin/codex"]
      cwd: /abs/path/repo
    claude:
      command: ["/usr/local/bin/claude"]
      cwd: /abs/path/repo
    glm:
      command: ["/usr/local/bin/claude", "--model", "glm-4.5"]
      cwd: /abs/path/repo
```

## Selección de proyecto

- Global por usuario vía `POST /api/projects/select`
- La API OpenAI-compatible usa esa selección cuando no se pasa `project` explícito.
