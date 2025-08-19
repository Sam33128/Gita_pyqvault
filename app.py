import os
import re
import json
from datetime import datetime
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect,
    url_for, send_from_directory, flash, session
)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__)
app.config["SECRET_KEY"] = "dev-key-change-later"  # change for production
app.config["UPLOAD_FOLDER"] = BASE_DIR / "uploads"
app.config["DATA_FILE"] = BASE_DIR / "data" / "papers.json"
app.config["ALLOWED_EXTENSIONS"] = {"pdf", "jpg", "jpeg", "png"}
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB per request
app.config["UPLOAD_PASSWORD"] = os.getenv("UPLOAD_PASSWORD", "odisha123")  # override via env var

# make session visible in Jinja templates
app.jinja_env.globals.update(session=session)

# ensure storage exists
app.config["UPLOAD_FOLDER"].mkdir(parents=True, exist_ok=True)
app.config["DATA_FILE"].parent.mkdir(parents=True, exist_ok=True)
if not app.config["DATA_FILE"].exists():
    app.config["DATA_FILE"].write_text("[]", encoding="utf-8")


# ---------- UTILITIES ----------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


def load_data():
    return json.loads(app.config["DATA_FILE"].read_text(encoding="utf-8"))


def save_data(data):
    app.config["DATA_FILE"].write_text(json.dumps(data, indent=2), encoding="utf-8")


def next_id(data):
    return (max((p["id"] for p in data), default=0) + 1)


def year_to_semesters(year: int):
    # 1st year -> 1,2 ; 2nd -> 3,4 ; 3rd -> 5,6 ; 4th -> 7,8
    return {1: [1, 2], 2: [3, 4], 3: [5, 6], 4: [7, 8]}.get(year, [])


def parse_exam_year(value: str):
    """
    Accepts '2024', '2024-25', '2024-2025' (also en/em dashes) and returns:
      (exam_year_int, academic_year_str or None)
    exam_year_int is the starting year (used for sorting).
    """
    s = (value or "").strip().replace("–", "-").replace("—", "-").replace(" ", "")
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        y = int(m.group(1))
        return y, None
    m = re.fullmatch(r"(\d{4})-(\d{2}|\d{4})", s)
    if m:
        start = int(m.group(1))
        end_part = m.group(2)
        end = int(end_part) if len(end_part) == 4 else int(str(start // 100 * 100 + int(end_part)))
        if end < start:
            end = start + 1
        return start, f"{start}-{end}"
    return None, None


def normalize_data_paths() -> int:
    """
    One-time healer: convert any backslashes in stored_path to forward slashes.
    Returns number of records changed.
    """
    data = load_data()
    changed = 0
    for p in data:
        sp = p.get("stored_path")
        if isinstance(sp, str):
            fixed = sp.replace("\\\\", "/").replace("\\", "/")
            if fixed != sp:
                p["stored_path"] = fixed
                changed += 1
    if changed:
        save_data(data)
    return changed


# Heal JSON paths at startup
try:
    healed = normalize_data_paths()
    if healed:
        print(f"[startup] normalized {healed} stored_path value(s) to forward slashes")
except Exception as e:
    print("[startup] normalize failed:", e)


# ---------- ROUTES ----------
@app.route("/")
def index():
    years = [1, 2, 3, 4]
    return render_template("index.html", years=years)


@app.route("/year/<int:year>")
def year_page(year):
    semesters = year_to_semesters(year)
    if not semesters:
        flash("Invalid year.", "warning")
        return redirect(url_for("index"))
    return render_template("year.html", year=year, semesters=semesters)


@app.route("/year/<int:year>/semester/<int:semester>")
def semester_page(year, semester):
    data = load_data()
    subjects = sorted({p["subject"] for p in data if p["year"] == year and p["semester"] == semester})
    return render_template("semester.html", year=year, semester=semester, subjects=subjects)


# ---------- SEARCHABLE PAPERS LIST ----------
@app.route("/papers")
def papers_list():
    data = load_data()

    # Read filters (all optional)
    year_str = (request.args.get("year") or "").strip()
    semester_str = (request.args.get("semester") or "").strip()
    subject = (request.args.get("subject") or "").strip()
    exam_type = (request.args.get("exam_type") or "").strip()

    # Convert numeric filters safely
    try:
        year = int(year_str) if year_str else None
    except ValueError:
        year = None
    try:
        semester = int(semester_str) if semester_str else None
    except ValueError:
        semester = None

    # Filter
    filtered = []
    for p in data:
        if year is not None and p["year"] != year:
            continue
        if semester is not None and p["semester"] != semester:
            continue
        if subject and p["subject"].lower() != subject.lower():
            continue
        if exam_type and p["exam_type"] != exam_type:
            continue
        filtered.append(p)

    # Sort newest first
    filtered.sort(key=lambda x: (-x["exam_year"], x["subject"], x["exam_type"]))

    # Subject suggestions (respect chosen year/semester if provided)
    subjects_all = sorted({
        p["subject"] for p in data
        if (year is None or p["year"] == year) and (semester is None or p["semester"] == semester)
    })

    return render_template(
        "papers.html",
        year=year or "",
        semester=semester or "",
        subject=subject,
        exam_type=exam_type,
        papers=filtered,
        subjects_all=subjects_all
    )


# ---------- SIMPLE ADMIN LOGIN ----------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = (request.form.get("password") or "").strip()
        if password == app.config["UPLOAD_PASSWORD"]:
            session["is_admin"] = True
            flash("Logged in successfully.", "success")
            next_page = request.args.get("next") or url_for("upload")
            return redirect(next_page)
        else:
            flash("Incorrect password.", "danger")
            return redirect(url_for("admin_login"))
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("Logged out.", "success")
    return redirect(url_for("index"))


# ---------- UPLOAD (PASSWORD-PROTECTED, MULTI-FILE, DUPLICATE HANDLING) ----------
@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login", next=url_for("upload")))

    if request.method == "POST":
        subject = (request.form.get("subject") or "").strip()
        exam_type = (request.form.get("exam_type") or "").strip()  # "Mid" or "End"

        # Year & Semester must be ints
        try:
            year = int(request.form.get("year") or "0")
            semester = int(request.form.get("semester") or "0")
        except ValueError:
            flash("Year/semester must be numbers.", "danger")
            return redirect(url_for("upload"))

        # Exam year can be range (e.g., 2024-25)
        raw_exam_year = (request.form.get("exam_year") or "").strip()
        exam_year, academic_year = parse_exam_year(raw_exam_year)
        if not exam_year or exam_year < 2000:
            flash("Please enter a valid year like 2024 or 2024-25.", "warning")
            return redirect(url_for("upload"))

        # Collect multiple files
        files = request.files.getlist("files")
        files = [f for f in files if f and f.filename]  # drop empties

        if not files:
            flash("Please choose at least one file.", "warning")
            return redirect(url_for("upload"))

        if not subject or exam_type not in {"Mid", "End"} or year not in {1, 2, 3, 4} or semester not in range(1, 9):
            flash("Please fill all fields correctly.", "warning")
            return redirect(url_for("upload"))

        target_dir = app.config["UPLOAD_FOLDER"] / str(year) / str(semester) / subject / exam_type
        target_dir.mkdir(parents=True, exist_ok=True)

        data = load_data()
        saved_count = 0
        skipped_count = 0
        warnings = []

        for f in files:
            if not allowed_file(f.filename):
                warnings.append(f"Skipped unsupported type: {f.filename}")
                skipped_count += 1
                continue

            safe_name = secure_filename(f.filename)
            file_path = target_dir / safe_name

            # If a file with the same name exists, delete the old duplicate
            if file_path.exists():
                try:
                    file_path.unlink()
                except Exception as e:
                    warnings.append(f"Couldn't remove old duplicate for {safe_name}: {e}")
                    skipped_count += 1
                    continue

            # Save the new file
            f.save(file_path)

            record = {
                "id": next_id(data),
                "year": year,
                "semester": semester,
                "subject": subject,
                "exam_type": exam_type,          # Mid (25) or End (100)
                "exam_year": exam_year,          # int: starting year
                "academic_year": academic_year,  # str: '2024-2025' if provided
                "original_filename": f.filename,
                # CRITICAL: store forward slashes so URLs work cross-platform
                "stored_path": file_path.relative_to(app.config["UPLOAD_FOLDER"]).as_posix(),
                "uploaded_at": datetime.now().isoformat(timespec="seconds"),
            }
            data.append(record)
            saved_count += 1

        save_data(data)

        if saved_count:
            flash(f"Uploaded {saved_count} file(s) successfully.", "success")
        if skipped_count or warnings:
            flash(" ".join(warnings) or f"Skipped {skipped_count} file(s).", "warning")

        return redirect(url_for("papers_list", year=year, semester=semester, subject=subject))

    return render_template("upload.html")


# ---------- DELETE ----------
@app.post("/admin/delete/<int:paper_id>")
def delete_paper(paper_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_login", next=url_for("papers_list")))

    data = load_data()
    record = next((p for p in data if p["id"] == paper_id), None)
    next_url = request.form.get("next") or url_for("papers_list")

    if not record:
        flash("Paper not found.", "warning")
        return redirect(next_url)

    rel = record.get("stored_path")
    if rel:
        try:
            (app.config["UPLOAD_FOLDER"] / rel).unlink(missing_ok=True)
        except Exception as e:
            flash(f"Couldn't remove file from disk: {e}", "warning")

    data = [p for p in data if p["id"] != paper_id]
    save_data(data)

    flash("Paper deleted.", "success")
    return redirect(next_url)


# ---------- FILE SERVE (normalize slashes) ----------
@app.route("/files/<path:relpath>")
def serve_file(relpath):
    # normalize any backslashes or accidental doubles
    relpath = relpath.replace("\\", "/").replace("//", "/").lstrip("/")
    return send_from_directory(app.config["UPLOAD_FOLDER"], relpath, as_attachment=False)


if __name__ == "__main__":
    print("STATIC FOLDER ->", app.static_folder)
    print("UPLOAD FOLDER ->", app.config["UPLOAD_FOLDER"])
    app.run(debug=True)
