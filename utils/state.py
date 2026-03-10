import threading

class AgentState:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._data = {
                        "status": "idle",
                        "suggestions": [],
                        "account": {},
                        "positions": [],
                        "history": [],
                    }
        return cls._instance

    def get_status(self): return self._data["status"]
    def set_status(self, s): self._data["status"] = s
    def get_latest_suggestions(self): return self._data["suggestions"]
    def set_latest_suggestions(self, s): self._data["suggestions"] = s
    def get_pending_suggestions(self):
        return [s for s in self._data["suggestions"] if s.get("status") == "PENDING_APPROVAL"]
    def get_account(self): return self._data["account"]
    def set_account(self, a): self._data["account"] = a
    def get_positions(self): return self._data["positions"]
    def set_positions(self, p): self._data["positions"] = p
    def get_trade_history(self): return self._data["history"]
    def add_to_history(self, t): self._data["history"].insert(0, t)
    def update_suggestion_status(self, symbol, status, extra=None):
        for s in self._data["suggestions"]:
            if s.get("symbol") == symbol:
                s["status"] = status
                if extra:
                    s.update(extra)
                break
