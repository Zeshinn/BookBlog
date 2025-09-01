from fastapi import FastAPI, HTTPException, Depends, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Annotated
import models
from database import engine, SessionLocal
from sqlalchemy.orm import Session
import datetime, shutil, os
from sqlalchemy import desc
from passlib.context import CryptContext
from PIL import Image
import time
from google.cloud import storage
import json
from dotenv import load_dotenv

load_dotenv()

# Initialize GCS client
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
storage_client = storage.Client.from_service_account_info(creds_dict)
bucket_name = "bakurika"
bucket = storage_client.bucket(bucket_name)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


app = FastAPI(
    docs_url=None,        # disable Swagger UI
    redoc_url=None,       # disable ReDoc
    openapi_url=None      # disable OpenAPI schema
)
templates = Jinja2Templates(directory="templates")
models.Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

db_dependency = Annotated[Session, Depends(get_db)]


class UserCreate(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    class Config:
        orm_mode = True

class PostCreate(BaseModel):
    title: str
    text: str
    user_id: int

class PostResponse(BaseModel):
    id: int
    title: str
    text: str
    user_id: int
    created_at: datetime.datetime
    class Config:
        orm_mode = True



@app.get('/')
async def home(request: Request, db: db_dependency):
    latest_post = db.query(models.Posts).order_by(desc(models.Posts.created_at)).first()
    latest_song = db.query(models.Song).order_by(desc(models.Song.created_at)).first()
    if not latest_post :
         return templates.TemplateResponse('home.html',
                                            {
                                                "request": request,
                                                "title": "",
                                                "text": "",
                                                "author": "",
                                                "song_title": "",
                                                "group": "",
                                                "singer": "",
                                                "timestamp": int(time.time())
                                                })
    author = db.query(models.Users).filter(models.Users.id == latest_post.user_id).first()
    author_name = author.username if author else "Unknown"
    singer = db.query(models.Users).filter(models.Users.id == latest_song.user_id).first()
    singer_name = singer.username if author else "Unknown"
    return templates.TemplateResponse('home.html',
                                      {
                                          "request": request,
                                          "title": latest_post.title,
                                          "text": latest_post.text,
                                          "author": author_name,
                                          "song_title": latest_song.title,
                                          "group": latest_song.group,
                                          "singer": singer_name,
                                          "timestamp": int(time.time())
                                          })

@app.get('/archive')
async def archive(request: Request, db: db_dependency):
    posts = db.query(models.Posts).order_by(desc(models.Posts.created_at)).all()
    users_query= db.query(models.Users)
    posts_data = []
    for post in posts:
        author = users_query.filter(models.Users.id == post.user_id).first()
        posts_data.append({
            "title": post.title,
            "author": author.username if author else "Unknown",
            "created_at": post.created_at.strftime("%Y-%m-%d"),
            "id": post.id
        })

    return templates.TemplateResponse(
        "archive.html",
        {"request": request, "posts": posts_data}
    )

@app.get("/write")
async def get_blog_write(request: Request):
    return templates.TemplateResponse("blogWrite.html", {"request": request})

@app.post("/write")
async def blog_write(
    request: Request,
    db: db_dependency,
    title: str = Form(...),
    text: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
):
    # 1. Check if user exists
    user = db.query(models.Users).filter(models.Users.username == username).first()
    if not user or not verify_password(password, user.password):  # bcrypt check
        return templates.TemplateResponse(
            "blogWrite.html",
            {"request": request, "error": "Невалидни потребителско име или парола."},
            status_code=400
        )

    # 2. Create post
    db_post = models.Posts(
        title=title,
        text=text,
        user_id=user.id,
        created_at=datetime.datetime.now()
    )
    db.add(db_post)
    db.commit()
    db.refresh(db_post)

    # 3. Redirect to success page
    return RedirectResponse(url="/", status_code=303)


@app.get("/song")
async def get_song(request: Request):
    return templates.TemplateResponse("song.html", {"request": request})


@app.get('/blog/{post_id}')
async def blog(request: Request, post_id: int, db: db_dependency):
    post = db.query(models.Posts).filter(models.Posts.id == post_id).first()
    author = db.query(models.Users).filter(models.Users.id == post.user_id).first()
    author_name = author.username if author else "Unknown"
    return templates.TemplateResponse(
        "post.html",
        {
            "request": request,
            "title": post.title,
            "text": post.text,
            "author": author_name,
            "created_at": post.created_at.strftime("%Y-%m-%d")
        })

@app.get("/write")
async def get_blog_write(request: Request):
    return templates.TemplateResponse("blogWrite.html", {"request": request})

@app.post("/song")
async def create_song(
    request: Request,
    db: db_dependency,
    title: str = Form(...),
    text: str = Form(...),
    image: UploadFile = File(...),
    username: str = Form(...),
    password: str = Form(...),
):
    # 1. Check if user exists
    user = db.query(models.Users).filter(models.Users.username == username).first()
    if not user or not verify_password(password, user.password):
        return templates.TemplateResponse(
            "song.html",
            {"request": request, "error": "Невалидни потребителско име или парола."},
            status_code=400
        )

    # 2. Upload to Google Cloud Storage (overwrite song.jpg)
    blob = bucket.blob("songs/song.jpg")  # fixed name → overwrites
    image.file.seek(0)  # reset file pointer before upload
    blob.upload_from_file(image.file, content_type=image.content_type)

    # 3. Save DB entry with public URL
    db_song = models.Song(
        title=title,
        group=text,
        image=f"https://storage.googleapis.com/{bucket_name}/songs/song.jpg",  # fixed public URL
        user_id=user.id,
        created_at=datetime.datetime.now()
    )
    db.add(db_song)
    db.commit()
    db.refresh(db_song)

    return RedirectResponse(url="/", status_code=303)

@app.head("/ping")
async def ping_head():
    return {}

