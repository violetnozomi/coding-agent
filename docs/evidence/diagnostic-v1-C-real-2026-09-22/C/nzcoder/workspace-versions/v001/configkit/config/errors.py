class ConfigError(ValueError):
    """A configuration problem, identified by a JSON path and a machine code."""

    def __init__(self, path, code="invalid"):
        self.path = path
        self.code = code
        self.issues = [{"path": path, "code": code}]
        super().__init__(f"{path}: {code}")

    def to_dict(self):
        return {"error": "invalid_config", "issues": [dict(issue) for issue in self.issues]}
