class ConfigError(ValueError):
    def __init__(self, path, code="invalid"):
        self.path, self.code = path, code
        super().__init__(f"{path}: {code}")

    def to_dict(self):
        return {"error": "invalid_config", "issues": [{"path": self.path, "code": self.code}]}
