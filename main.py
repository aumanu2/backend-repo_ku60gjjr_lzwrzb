import os
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from bson import ObjectId
from datetime import datetime

from database import db, create_document, get_documents

app = FastAPI(title="ROOMANCE API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Utility ----------

def to_oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id format")


def serialize(doc: dict):
    if not doc:
        return None
    d = doc.copy()
    _id = d.pop("_id", None)
    if _id:
        d["id"] = str(_id)
    # Convert datetime to isoformat
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d

# ---------- Schemas ----------

class SignupRequest(BaseModel):
    email: str
    password: str

class AuthResponse(BaseModel):
    user_id: str
    email: str

class LoginRequest(BaseModel):
    email: str
    password: str

class ProfileDetails(BaseModel):
    user_id: str
    nickname: str
    bio: Optional[str] = ""
    tags: List[str] = []
    photos: List[str] = []  # store URLs or base64 (demo)
    age: Optional[int] = None

class ProfileUpdate(BaseModel):
    user_id: str
    nickname: Optional[str] = None
    bio: Optional[str] = None
    tags: Optional[List[str]] = None
    photos: Optional[List[str]] = None
    age: Optional[int] = None

class LikeRequest(BaseModel):
    user_id: str
    target_id: str
    action: str = Field(..., pattern="^(like|dislike)$")

class SendMessageRequest(BaseModel):
    user_id: str
    peer_id: str
    text: str

# ---------- Basic ----------

@app.get("/")
def read_root():
    return {"message": "ROOMANCE backend is live"}

@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:80]}"
        else:
            response["database"] = "⚠️ Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response

# ---------- Auth ----------

@app.post("/auth/signup", response_model=AuthResponse)
def signup(payload: SignupRequest):
    # check existing
    existing = db["user"].find_one({"email": payload.email}) if db else None
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user_doc = {
        "email": payload.email,
        "password": payload.password,  # demo only; not for production
    }
    user_id = db["user"].insert_one(user_doc).inserted_id
    return {"user_id": str(user_id), "email": payload.email}

@app.post("/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest):
    user = db["user"].find_one({"email": payload.email, "password": payload.password}) if db else None
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"user_id": str(user["_id"]), "email": user["email"]}

# ---------- Profile ----------

@app.post("/profile/details")
def create_or_complete_profile(details: ProfileDetails):
    user_oid = to_oid(details.user_id)
    # upsert
    update = {
        "$set": {
            "user_id": user_oid,
            "nickname": details.nickname,
            "bio": details.bio,
            "tags": details.tags,
            "photos": details.photos,
            "age": details.age,
            "updated_at": datetime.utcnow(),
        },
        "$setOnInsert": {"created_at": datetime.utcnow()},
    }
    db["profile"].update_one({"user_id": user_oid}, update, upsert=True)
    prof = db["profile"].find_one({"user_id": user_oid})
    return serialize(prof)

@app.get("/profile/me")
def get_my_profile(user_id: str):
    user_oid = to_oid(user_id)
    prof = db["profile"].find_one({"user_id": user_oid})
    if not prof:
        raise HTTPException(status_code=404, detail="Profile not found")
    return serialize(prof)

@app.post("/profile/update")
def update_profile(payload: ProfileUpdate):
    user_oid = to_oid(payload.user_id)
    changes = {k: v for k, v in payload.model_dump().items() if k not in ("user_id",) and v is not None}
    if not changes:
        raise HTTPException(status_code=400, detail="No changes provided")
    changes["updated_at"] = datetime.utcnow()
    res = db["profile"].update_one({"user_id": user_oid}, {"$set": changes})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Profile not found")
    prof = db["profile"].find_one({"user_id": user_oid})
    return serialize(prof)

# ---------- Discovery / Likes ----------

@app.get("/profiles/next")
def next_profile(user_id: str):
    user_oid = to_oid(user_id)
    # exclude self and already acted upon
    acted_ids = set()
    for m in db["match"].find({"user_id": user_oid}):
        acted_ids.add(m.get("target_id"))
    query = {
        "user_id": {"$ne": user_oid}
    }
    candidates = db["profile"].find(query).sort("updated_at", -1)
    for cand in candidates:
        if cand["user_id"] not in acted_ids:
            return serialize(cand)
    return {"message": "No more profiles"}

@app.post("/profiles/like")
def like_profile(payload: LikeRequest):
    user_oid = to_oid(payload.user_id)
    target_oid = to_oid(payload.target_id)
    if user_oid == target_oid:
        raise HTTPException(status_code=400, detail="Cannot like yourself")
    # record action
    db["match"].update_one(
        {"user_id": user_oid, "target_id": target_oid},
        {"$set": {"action": payload.action, "updated_at": datetime.utcnow()}, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
    )
    matched = False
    if payload.action == "like":
        # check reciprocal like
        other = db["match"].find_one({"user_id": target_oid, "target_id": user_oid, "action": "like"})
        matched = other is not None
    return {"matched": matched}

# ---------- Chats ----------

@app.get("/chats/list")
def list_chats(user_id: str):
    user_oid = to_oid(user_id)
    # find mutual likes
    my_likes = db["match"].find({"user_id": user_oid, "action": "like"})
    peers = []
    for like in my_likes:
        other_like = db["match"].find_one({"user_id": like["target_id"], "target_id": user_oid, "action": "like"})
        if other_like:
            peer_prof = db["profile"].find_one({"user_id": like["target_id"]})
            if peer_prof:
                peers.append(serialize(peer_prof))
    return peers

@app.get("/chats/messages")
def get_messages(user_id: str, peer_id: str, limit: int = 50):
    u = to_oid(user_id)
    p = to_oid(peer_id)
    q = {"$or": [
        {"sender_id": u, "receiver_id": p},
        {"sender_id": p, "receiver_id": u},
    ]}
    msgs = list(db["message"].find(q).sort("created_at", 1).limit(limit))
    return [serialize(m) for m in msgs]

@app.post("/chats/send")
def send_message(payload: SendMessageRequest):
    u = to_oid(payload.user_id)
    p = to_oid(payload.peer_id)
    doc = {
        "sender_id": u,
        "receiver_id": p,
        "text": payload.text,
        "created_at": datetime.utcnow(),
    }
    mid = db["message"].insert_one(doc).inserted_id
    saved = db["message"].find_one({"_id": mid})
    return serialize(saved)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
