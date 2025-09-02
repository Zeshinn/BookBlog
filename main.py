from fastapi import FastAPI, HTTPException, Depends, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Annotated
import models
from database import engine, SessionLocal
from sqlalchemy.orm import Session
import datetime, shutil, os, re, requests, io
from sqlalchemy import desc
from passlib.context import CryptContext
from PIL import Image
import time
from google.cloud import storage
import json
from dotenv import load_dotenv
from bs4 import BeautifulSoup

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

def get_spotify_metadata(spotify_url: str):
    """Fetch title, artist, and album cover from a Spotify link (no auth)."""

    # 1. Try oEmbed first
    api_url = "https://open.spotify.com/oembed"
    resp = requests.get(api_url, params={"url": spotify_url})
    resp.raise_for_status()
    data = resp.json()

    title = data.get("title")
    artist = data.get("author_name")  # may be missing
    thumbnail = data.get("thumbnail_url")

    # 2. If author_name or thumbnail missing → fallback to scraping
    if not artist or not thumbnail:
        page = requests.get(spotify_url, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(page.text, "html.parser")

        if not title:
            title = soup.find("meta", property="og:title")
            if title: 
                title = title["content"]

        if not artist:
            desc = soup.find("meta", property="og:description")
            if desc:
                # "Rick Astley · Song · 1987" → take first part before " · "
                artist = desc["content"].split("·")[0].strip()

        if not thumbnail:
            og_image = soup.find("meta", property="og:image")
            if og_image:
                thumbnail = og_image["content"]

    # 3. Upgrade cover to largest size if possible
    high_res = None
    if thumbnail:
        high_res = re.sub(r"(\d{3,4})x(\d{3,4})", "1200x1200", thumbnail)

    return {
        "title": title or "Unknown Title",
        "artist": artist or "Unknown Artist",
        "album_cover": high_res,
        "spotify_link": spotify_url
    }

@app.post("/song")
async def create_song(
    request: Request,
    db: db_dependency,
    mode: str = Form(...),  # <-- get mode (spotify/manual)
    title: str = Form(None),
    text: str = Form(None),
    image: UploadFile = File(None),
    spotify_url: str = Form(None),
    username: str = Form(...),
    password: str = Form(...),
):
    # 1. Check user
    user = db.query(models.Users).filter(models.Users.username == username).first()
    if not user or not verify_password(password, user.password):
        return templates.TemplateResponse(
            "song.html",
            {"request": request, "error": "Невалидни потребителско име или парола."},
            status_code=400
        )

    # 2. Handle based on mode
    if mode == "spotify":
        if not spotify_url:
            return templates.TemplateResponse(
                "song.html",
                {"request": request, "error": "Моля, въведи Spotify линк."},
                status_code=400
            )

        meta = get_spotify_metadata(spotify_url)
        song_title = meta["title"]
        song_group = meta["artist"]
        cover_url = meta["album_cover"]
        song_image = None
        if cover_url:
            resp = requests.get(cover_url, stream=True)
            resp.raise_for_status()
            blob = bucket.blob("songs/song.jpg")  # ⚠️ still overwrites!
            blob.upload_from_file(io.BytesIO(resp.content), content_type="image/jpeg")
            song_image = f"https://storage.googleapis.com/{bucket_name}/songs/song.jpg"
    elif mode == "manual":
        if not (title and text and image):
            return templates.TemplateResponse(
                "song.html",
                {"request": request, "error": "Моля, въведи ръчно име, изпълнител и снимка."},
                status_code=400
            )

        song_title = title
        song_group = text

        # Upload provided image to Google Cloud Storage
        blob = bucket.blob("songs/song.jpg")  # ⚠️ fixed name → overwrites
        image.file.seek(0)
        blob.upload_from_file(image.file, content_type=image.content_type)
        song_image = f"https://storage.googleapis.com/{bucket_name}/songs/song.jpg"

    else:
        return templates.TemplateResponse(
            "song.html",
            {"request": request, "error": "Невалиден избор за начин на въвеждане."},
            status_code=400
        )

    # 3. Save DB entry
    db_song = models.Song(
        title=song_title,
        group=song_group,
        image=song_image,
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

