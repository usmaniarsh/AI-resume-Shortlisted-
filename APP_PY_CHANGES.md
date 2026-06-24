# app.py mein ye changes karo

## STEP 1 — `load_data()` function ko replace karo

**PURANA CODE (dhundo aur replace karo):**
```python
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"jobs": [], "applications": []}
```

**NAYA CODE:**
```python
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("jobs", [])
    data.setdefault("applications", [])
    data.setdefault("walkin_drives", [])
    data.setdefault("walkin_registrations", [])
    return data
```
(Ye purane data.json files ke saath bhi safe hai — naye keys khud add ho jayenge.)

---

## STEP 2 — Purana `/walk-in` route DELETE karo

Ye wala block dhundo aur **pura hata do** (isko hum naye routes se replace karenge — niche STEP 4 mein):

```python
@app.route("/walk-in")
def walk_in():
    data = load_data()
    active_jobs = [j for j in data["jobs"] if j.get("active", True)]
    applicant_counts = {}
    for a in data["applications"]:
        applicant_counts[a["job_id"]] = applicant_counts.get(a["job_id"], 0) + 1
    return render_template("walk_in.html", jobs=active_jobs, applicant_counts=applicant_counts)
```

---

## STEP 3 — `admin_home()` route ko replace karo

**PURANA CODE:**
```python
@app.route("/admin/home")
@admin_required
def admin_home():
    data = load_data()
    total_apps = len(data["applications"])
    shortlisted = sum(1 for a in data["applications"] if a.get("ai_analysis", {}).get("status") == "Shortlisted")
    on_hold = sum(1 for a in data["applications"] if a.get("ai_analysis", {}).get("status") == "On Hold")
    rejected = sum(1 for a in data["applications"] if a.get("ai_analysis", {}).get("status") == "Rejected")
    stats = {
        "total": total_apps,
        "shortlisted": shortlisted,
        "on_hold": on_hold,
        "rejected": rejected,
        "active_jobs": sum(1 for j in data["jobs"] if j.get("active", True)),
    }

    # Har job ke liye applicant count calculate karo
    applicant_counts = {}
    for a in data["applications"]:
        applicant_counts[a["job_id"]] = applicant_counts.get(a["job_id"], 0) + 1

    # Har job object mein applicant_count field inject karo
    jobs = data["jobs"]
    for job in jobs:
        job["applicant_count"] = applicant_counts.get(job["id"], 0)

    return render_template("admin_dashboard.html", stats=stats, jobs=jobs)
```

**NAYA CODE:**
```python
@app.route("/admin/home")
@admin_required
def admin_home():
    data = load_data()
    total_apps = len(data["applications"])
    shortlisted = sum(1 for a in data["applications"] if a.get("ai_analysis", {}).get("status") == "Shortlisted")
    on_hold = sum(1 for a in data["applications"] if a.get("ai_analysis", {}).get("status") == "On Hold")
    rejected = sum(1 for a in data["applications"] if a.get("ai_analysis", {}).get("status") == "Rejected")
    stats = {
        "total": total_apps,
        "shortlisted": shortlisted,
        "on_hold": on_hold,
        "rejected": rejected,
        "active_jobs": sum(1 for j in data["jobs"] if j.get("active", True)),
        "active_drives": sum(1 for d in data["walkin_drives"] if d.get("active", True)),
    }

    # Har job ke liye applicant count calculate karo
    applicant_counts = {}
    for a in data["applications"]:
        applicant_counts[a["job_id"]] = applicant_counts.get(a["job_id"], 0) + 1

    # Har job object mein applicant_count field inject karo
    jobs = data["jobs"]
    for job in jobs:
        job["applicant_count"] = applicant_counts.get(job["id"], 0)

    # Har walk-in drive ke liye registration count calculate karo
    reg_counts = {}
    for r in data["walkin_registrations"]:
        reg_counts[r["drive_id"]] = reg_counts.get(r["drive_id"], 0) + 1

    walkin_drives = data["walkin_drives"]
    for drive in walkin_drives:
        drive["registration_count"] = reg_counts.get(drive["id"], 0)

    return render_template("admin_dashboard.html", stats=stats, jobs=jobs, walkin_drives=walkin_drives)
```

---

## STEP 4 — Ye NAYE routes add karo (kahin bhi "ADMIN PORTAL" section ke andar, ya end mein `if __name__` se pehle paste kar do)

```python
# ============ WALK-IN DRIVE — ADMIN ============

@app.route("/admin/walkin/new", methods=["GET", "POST"])
@admin_required
def new_walkin():
    if request.method == "POST":
        data = load_data()
        drive = {
            "id": str(uuid.uuid4()),
            "title": request.form.get("title", "").strip(),
            "department": request.form.get("department", "").strip(),
            "location": request.form.get("location", "").strip(),
            "venue": request.form.get("venue", "").strip(),
            "drive_date": request.form.get("drive_date", "").strip(),
            "drive_time": request.form.get("drive_time", "").strip(),
            "description": request.form.get("description", "").strip(),
            "active": True,
            "created_at": datetime.now().isoformat(),
        }
        if not drive["title"] or not drive["drive_date"]:
            flash("Title and Date are mandatory.", "error")
            return render_template("new_walkin.html")
        data["walkin_drives"].append(drive)
        save_data(data)
        flash(f"Walk-in Drive '{drive['title']}' posted successfully!", "success")
        return redirect(url_for("admin_home"))
    return render_template("new_walkin.html")


@app.route("/admin/walkin/<drive_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_walkin(drive_id):
    data = load_data()
    drive = next((d for d in data["walkin_drives"] if d["id"] == drive_id), None)
    if not drive:
        flash("Walk-in Drive not found.", "error")
        return redirect(url_for("admin_home"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        drive_date = request.form.get("drive_date", "").strip()
        if not title or not drive_date:
            flash("Title and Date are mandatory.", "error")
            return render_template("edit_walkin.html", drive=drive)

        drive["title"] = title
        drive["department"] = request.form.get("department", "").strip()
        drive["location"] = request.form.get("location", "").strip()
        drive["venue"] = request.form.get("venue", "").strip()
        drive["drive_date"] = drive_date
        drive["drive_time"] = request.form.get("drive_time", "").strip()
        drive["description"] = request.form.get("description", "").strip()

        save_data(data)
        flash(f"Walk-in Drive '{drive['title']}' updated successfully!", "success")
        return redirect(url_for("admin_home"))

    return render_template("edit_walkin.html", drive=drive)


@app.route("/admin/walkin/<drive_id>/toggle", methods=["POST"])
@admin_required
def toggle_walkin(drive_id):
    data = load_data()
    for d in data["walkin_drives"]:
        if d["id"] == drive_id:
            d["active"] = not d.get("active", True)
            break
    save_data(data)
    return redirect(url_for("admin_home"))


@app.route("/admin/walkin/<drive_id>/delete", methods=["POST"])
@admin_required
def delete_walkin(drive_id):
    data = load_data()
    data["walkin_drives"] = [d for d in data["walkin_drives"] if d["id"] != drive_id]
    data["walkin_registrations"] = [r for r in data["walkin_registrations"] if r["drive_id"] != drive_id]
    save_data(data)
    flash("Walk-in Drive deleted.", "success")
    return redirect(url_for("admin_home"))


@app.route("/admin/walkin/<drive_id>/registrations")
@admin_required
def view_walkin_registrations(drive_id):
    data = load_data()
    drive = next((d for d in data["walkin_drives"] if d["id"] == drive_id), None)
    if not drive:
        flash("Walk-in Drive not found.", "error")
        return redirect(url_for("admin_home"))
    regs = [r for r in data["walkin_registrations"] if r["drive_id"] == drive_id]
    regs = sorted(regs, key=lambda r: r["registered_at"], reverse=True)
    return render_template("admin_walkin_registrations.html", drive=drive, registrations=regs)


@app.route("/admin/walkin/<drive_id>/registrations/delete-all", methods=["POST"])
@admin_required
def delete_all_walkin_registrations(drive_id):
    data = load_data()
    data["walkin_registrations"] = [r for r in data["walkin_registrations"] if r["drive_id"] != drive_id]
    save_data(data)
    flash("All registrations cleared.", "success")
    return redirect(url_for("view_walkin_registrations", drive_id=drive_id))


@app.route("/admin/walkin/registrations/<reg_id>/delete", methods=["POST"])
@admin_required
def delete_walkin_registration(reg_id):
    data = load_data()
    reg = next((r for r in data["walkin_registrations"] if r["id"] == reg_id), None)
    drive_id = reg["drive_id"] if reg else None
    data["walkin_registrations"] = [r for r in data["walkin_registrations"] if r["id"] != reg_id]
    save_data(data)
    flash("Registration deleted.", "success")
    if drive_id:
        return redirect(url_for("view_walkin_registrations", drive_id=drive_id))
    return redirect(url_for("admin_home"))


# ============ WALK-IN DRIVE — CANDIDATE FACING ============

@app.route("/walk-in")
def walk_in():
    data = load_data()
    active_drives = [d for d in data["walkin_drives"] if d.get("active", True)]
    reg_counts = {}
    for r in data["walkin_registrations"]:
        reg_counts[r["drive_id"]] = reg_counts.get(r["drive_id"], 0) + 1
    return render_template("walk_in.html", drives=active_drives, reg_counts=reg_counts)


@app.route("/walk-in/<drive_id>/register", methods=["GET", "POST"])
def register_walkin(drive_id):
    data = load_data()
    drive = next((d for d in data["walkin_drives"] if d["id"] == drive_id), None)
    if not drive:
        flash("Walk-in Drive not found.", "error")
        return redirect(url_for("walk_in"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        slot = request.form.get("slot", "").strip()

        if not name or not email or not phone:
            flash("Please fill all required fields.", "error")
            return render_template("walkin_register.html", drive=drive)

        already = any(
            r["drive_id"] == drive_id and r["email"].strip().lower() == email.lower()
            for r in data["walkin_registrations"]
        )
        if already:
            flash("You have already registered for this drive with this email.", "error")
            return render_template("walkin_register.html", drive=drive)

        registration = {
            "id": str(uuid.uuid4()),
            "drive_id": drive_id,
            "drive_title": drive["title"],
            "name": name,
            "email": email,
            "phone": phone,
            "slot": slot,
            "registered_at": datetime.now().isoformat(),
        }
        data["walkin_registrations"].append(registration)
        save_data(data)
        return render_template("walkin_registered.html", registration=registration, drive=drive)

    return render_template("walkin_register.html", drive=drive)
```

---

## Summary of new URLs created
| URL | Use |
|---|---|
| `/admin/walkin/new` | HR: post new walk-in drive |
| `/admin/walkin/<id>/edit` | HR: edit drive |
| `/admin/walkin/<id>/toggle` | HR: pause/activate |
| `/admin/walkin/<id>/delete` | HR: delete drive |
| `/admin/walkin/<id>/registrations` | HR: view all registered candidates |
| `/walk-in` | Candidate: see all open drives |
| `/walk-in/<id>/register` | Candidate: register (Naam, Email, Phone, Date slot) |
