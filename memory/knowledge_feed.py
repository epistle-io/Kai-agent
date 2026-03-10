"""memory/knowledge_feed.py — RAG knowledge base for KAI"""
import os, sqlite3
from datetime import datetime
from utils.logger import log

DB_PATH = os.path.join(os.path.dirname(__file__), "kai_memory.db")
KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_tables():
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL, chunk_index INTEGER,
        content TEXT NOT NULL, word_count INTEGER,
        tags TEXT, added_at TEXT)""")
    conn.commit(); conn.close()

def add_knowledge(text, source="manual", tags=""):
    init_tables()
    words = text.split()
    chunks = []
    for i in range(0, len(words), 350):
        chunk = " ".join(words[i:i+400])
        if len(chunk.strip()) > 50: chunks.append(chunk.strip())
    conn = get_db()
    conn.execute("DELETE FROM knowledge_chunks WHERE source=?", (source,))
    for i, chunk in enumerate(chunks):
        conn.execute("INSERT INTO knowledge_chunks (source,chunk_index,content,word_count,tags,added_at) VALUES (?,?,?,?,?,?)",
            (source,i,chunk,len(chunk.split()),tags,datetime.now().isoformat()))
    conn.commit(); conn.close()
    log("info", f"Knowledge: Added {len(chunks)} chunks from '{source}'")
    return len(chunks)

def load_knowledge_folder():
    init_tables()
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    loaded = []
    for filename in os.listdir(KNOWLEDGE_DIR):
        filepath = os.path.join(KNOWLEDGE_DIR, filename)
        if filename.endswith((".txt",".md")):
            try:
                text = open(filepath, encoding="utf-8").read()
                n = add_knowledge(text, source=filename, tags="document")
                loaded.append(f"{filename} ({n} chunks)")
            except Exception as e:
                log("error", f"Knowledge load failed {filename}: {e}")
        elif filename.endswith(".pdf"):
            try:
                from pypdf import PdfReader
                text = "".join(p.extract_text() + "\n" for p in PdfReader(filepath).pages)
                n = add_knowledge(text, source=filename, tags="pdf")
                loaded.append(f"{filename} ({n} chunks)")
            except ImportError:
                log("warning", "pypdf not installed — skipping PDF")
            except Exception as e:
                log("error", f"PDF load failed {filename}: {e}")
    return loaded

def search_knowledge(query, top_k=3):
    init_tables()
    conn = get_db()
    chunks = conn.execute("SELECT content, source FROM knowledge_chunks").fetchall()
    conn.close()
    if not chunks: return ""
    stop = {"the","a","an","is","in","on","at","to","for","of","and","or","but","with"}
    qwords = set(query.lower().split()) - stop
    scored = [(len(qwords & set(c["content"].lower().split())), c["content"], c["source"])
              for c in chunks if len(qwords & set(c["content"].lower().split())) > 0]
    if not scored: return ""
    scored.sort(reverse=True)
    return "RELEVANT KNOWLEDGE:\n" + "\n\n".join(f"[{s}]\n{c}" for _,c,s in scored[:top_k])

def get_knowledge_stats():
    init_tables()
    conn = get_db()
    rows = conn.execute("SELECT source, COUNT(*) as chunks, SUM(word_count) as words FROM knowledge_chunks GROUP BY source").fetchall()
    conn.close()
    return {"sources":[{"source":r["source"],"chunks":r["chunks"],"words":r["words"]} for r in rows],
            "total_chunks":sum(r["chunks"] for r in rows), "knowledge_dir": KNOWLEDGE_DIR}
