"""Share one store/lock per resolved root within the writer process."""

from __future__ import annotations

import threading
from organiser.services.knowledge import (
    KnowledgeStore,
    get_knowledge_store,
)


_SHARED_STORE_LOCK = threading.Lock()


_SHARED_STORES: dict[str, KnowledgeStore] = {}


def shared_knowledge_store(settings=None) -> KnowledgeStore:
    """Gemeinsam genutzte Store-Instanz pro Knowledge Root.

    Single-Writer/Single-Process-MVP: alle Schreibvorgaenge des Integrationspfads
    teilen sich eine Instanz und damit deren prozesslokales ``RLock``. Das ist
    ausreichend fuer den vorgesehenen Betrieb (ein Organiser-Prozess) und
    ausdruecklich keine Mehrprozess-Synchronisation.
    """
    store = get_knowledge_store(settings)
    key = str(store.root)
    with _SHARED_STORE_LOCK:
        existing = _SHARED_STORES.get(key)
        if existing is not None:
            return existing
        _SHARED_STORES[key] = store
        return store
