from fastapi import FastAPI, APIRouter, HTTPException, Query
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import csv
from io import StringIO
from fastapi.responses import StreamingResponse
import httpx


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Malaysia Timezone (UTC+8, no DST)
MYT_TZ = timezone(timedelta(hours=8))

def now_myt() -> datetime:
    """Current datetime in Malaysia Time (naive, so Mongo stores as-is)"""
    # Use naive datetime representing MYT wall time.
    # Motor/Mongo stores naive datetimes as-is (no UTC conversion),
    # keeping our times consistently in Malaysia Time.
    return (datetime.utcnow() + timedelta(hours=8))

def today_myt_str() -> str:
    """Today's date string in Malaysia Time (YYYY-MM-DD)"""
    return now_myt().strftime("%Y-%m-%d")

def days_ago_myt_str(days: int) -> str:
    """Date string N days ago in Malaysia Time"""
    return (now_myt() - timedelta(days=days)).strftime("%Y-%m-%d")

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Emergent-managed Resend email integration (proxy URL is a constant)
EMAIL_BASE_URL = "https://integrations.emergentagent.com"
EMERGENT_EMAIL_KEY = os.environ.get("EMERGENT_EMAIL_KEY", "")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "Package Tracker")

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Define Models
PACKAGE_STATUSES = ["pending", "picked_up", "returned"]

class Package(BaseModel):
    recipient_name: Optional[str] = None
    phone_number: Optional[str] = None
    tracking_number: Optional[str] = None
    notes: Optional[str] = None
    status: str = "pending"
    timestamp: datetime = Field(default_factory=now_myt)
    date: str = Field(default_factory=today_myt_str)

class PackageCreate(BaseModel):
    recipient_name: Optional[str] = None
    phone_number: Optional[str] = None
    tracking_number: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = "pending"

class PackageResponse(BaseModel):
    id: str
    recipient_name: Optional[str] = None
    phone_number: Optional[str] = None
    tracking_number: Optional[str] = None
    notes: Optional[str] = None
    status: str = "pending"
    timestamp: datetime
    date: str

class StatusUpdate(BaseModel):
    status: str

class BatchPickupItem(BaseModel):
    id: Optional[str] = None
    tracking_number: Optional[str] = None

class BatchStatusUpdate(BaseModel):
    items: List[BatchPickupItem]
    status: str


class BatchAddItem(BaseModel):
    tracking_number: Optional[str] = None
    recipient_name: Optional[str] = None
    phone_number: Optional[str] = None
    notes: Optional[str] = None


class BatchAddRequest(BaseModel):
    items: List[BatchAddItem]
    status: Optional[str] = "pending"


def _to_response(pkg: dict) -> "PackageResponse":
    """Build PackageResponse from a MongoDB document (excludes _id)."""
    return PackageResponse(
        id=str(pkg['_id']),
        recipient_name=pkg.get('recipient_name'),
        phone_number=pkg.get('phone_number'),
        tracking_number=pkg.get('tracking_number'),
        notes=pkg.get('notes'),
        status=pkg.get('status', 'pending'),
        timestamp=pkg['timestamp'],
        date=pkg['date'],
    )

class EarningUpdate(BaseModel):
    amount: float

class EarningResponse(BaseModel):
    date: str
    amount: float

class Goals(BaseModel):
    packages_goal: int = 100
    earnings_goal: float = 200.0

class GoalsUpdate(BaseModel):
    packages_goal: Optional[int] = None
    earnings_goal: Optional[float] = None

class DailyStats(BaseModel):
    date: str
    count: int
    earnings: float = 0.0

class StatsResponse(BaseModel):
    today: int
    week: int
    month: int
    today_charges: float
    week_charges: float
    month_charges: float
    today_pending: int = 0
    today_picked_up: int = 0
    today_returned: int = 0
    daily_breakdown: List[DailyStats]


# Routes
@api_router.get("/")
async def root():
    return {"message": "Package Tracker API"}


@api_router.post("/packages", response_model=PackageResponse)
async def create_package(package: PackageCreate):
    """Add a new package"""
    try:
        # Validate status
        status = package.status if package.status in PACKAGE_STATUSES else "pending"
        
        package_dict = package.dict()
        package_dict['status'] = status
        package_dict['timestamp'] = now_myt()
        package_dict['date'] = package_dict['timestamp'].strftime("%Y-%m-%d")
        
        result = await db.packages.insert_one(package_dict)
        
        package_dict['id'] = str(result.inserted_id)
        return PackageResponse(**package_dict)
    except Exception as e:
        logger.error(f"Error creating package: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/packages", response_model=List[PackageResponse])
async def get_packages(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(100, le=1000)
):
    """Get packages with optional filters"""
    try:
        query = {}
        
        # Date range filter
        if start_date or end_date:
            date_filter = {}
            if start_date:
                date_filter['$gte'] = start_date
            if end_date:
                date_filter['$lte'] = end_date
            query['date'] = date_filter
        
        # Status filter
        if status and status in PACKAGE_STATUSES:
            query['status'] = status
        
        # Search filter (search in recipient_name, phone_number, tracking_number, notes)
        if search:
            query['$or'] = [
                {'recipient_name': {'$regex': search, '$options': 'i'}},
                {'phone_number': {'$regex': search, '$options': 'i'}},
                {'tracking_number': {'$regex': search, '$options': 'i'}},
                {'notes': {'$regex': search, '$options': 'i'}}
            ]
        
        packages = await db.packages.find(query).sort('timestamp', -1).limit(limit).to_list(limit)
        
        return [
            PackageResponse(
                id=str(pkg['_id']),
                recipient_name=pkg.get('recipient_name'),
                phone_number=pkg.get('phone_number'),
                tracking_number=pkg.get('tracking_number'),
                notes=pkg.get('notes'),
                status=pkg.get('status', 'pending'),
                timestamp=pkg['timestamp'],
                date=pkg['date']
            )
            for pkg in packages
        ]
    except Exception as e:
        logger.error(f"Error fetching packages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/packages/stats", response_model=StatsResponse)
async def get_stats():
    """Get package statistics"""
    try:
        now = now_myt()
        today = today_myt_str()
        week_ago = days_ago_myt_str(7)
        month_ago = days_ago_myt_str(30)
        
        # Count today's packages
        today_count = await db.packages.count_documents({'date': today})
        
        # Count this week's packages
        week_count = await db.packages.count_documents({
            'date': {'$gte': week_ago}
        })
        
        # Count this month's packages
        month_count = await db.packages.count_documents({
            'date': {'$gte': month_ago}
        })
        
        # Get earnings from daily earnings collection
        today_earning = await db.earnings.find_one({'date': today})
        today_charges = today_earning.get('amount', 0) if today_earning else 0
        
        # Get week's earnings
        week_earnings = await db.earnings.find({'date': {'$gte': week_ago}}).to_list(100)
        week_charges = sum(e.get('amount', 0) for e in week_earnings)
        
        # Get month's earnings
        month_earnings = await db.earnings.find({'date': {'$gte': month_ago}}).to_list(100)
        month_charges = sum(e.get('amount', 0) for e in month_earnings)
        
        # Get today's status breakdown
        today_pending = await db.packages.count_documents({'date': today, 'status': 'pending'})
        today_picked_up = await db.packages.count_documents({'date': today, 'status': 'picked_up'})
        today_returned = await db.packages.count_documents({'date': today, 'status': 'returned'})
        
        # Get daily breakdown for the last 30 days
        pipeline = [
            {'$match': {'date': {'$gte': month_ago}}},
            {'$group': {
                '_id': '$date',
                'count': {'$sum': 1}
            }},
            {'$sort': {'_id': -1}}
        ]
        
        daily_data = await db.packages.aggregate(pipeline).to_list(30)
        
        # Build earnings map for the same window
        earnings_map = {e['date']: e.get('amount', 0) for e in month_earnings}
        
        # Collect all dates with either packages or earnings
        all_dates = set(item['_id'] for item in daily_data) | set(earnings_map.keys())
        counts_map = {item['_id']: item['count'] for item in daily_data}
        
        daily_breakdown = sorted(
            [
                DailyStats(
                    date=d,
                    count=counts_map.get(d, 0),
                    earnings=round(earnings_map.get(d, 0), 2),
                )
                for d in all_dates
            ],
            key=lambda x: x.date,
            reverse=True,
        )[:30]
        
        return StatsResponse(
            today=today_count,
            week=week_count,
            month=month_count,
            today_charges=round(today_charges, 2),
            week_charges=round(week_charges, 2),
            month_charges=round(month_charges, 2),
            today_pending=today_pending,
            today_picked_up=today_picked_up,
            today_returned=today_returned,
            daily_breakdown=daily_breakdown
        )
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/earnings/today", response_model=EarningResponse)
async def get_today_earnings():
    """Get today's earnings"""
    try:
        today = today_myt_str()
        earning = await db.earnings.find_one({'date': today})
        if earning:
            return EarningResponse(date=today, amount=earning.get('amount', 0))
        return EarningResponse(date=today, amount=0)
    except Exception as e:
        logger.error(f"Error fetching earnings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.put("/earnings/today", response_model=EarningResponse)
async def update_today_earnings(earning: EarningUpdate):
    """Set today's earnings (overwrites existing)"""
    try:
        today = today_myt_str()
        await db.earnings.update_one(
            {'date': today},
            {'$set': {'date': today, 'amount': earning.amount, 'updated_at': now_myt()}},
            upsert=True
        )
        return EarningResponse(date=today, amount=earning.amount)
    except Exception as e:
        logger.error(f"Error updating earnings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class BatchStatusResponse(BaseModel):
    updated: List[PackageResponse]
    skipped: List[PackageResponse]  # packages already in target status
    created: List[PackageResponse]  # newly created (didn't exist)


@api_router.get("/packages/lookup", response_model=Optional[PackageResponse])
async def lookup_package_by_tracking(tracking_number: str):
    """Look up a single package by tracking number.
    Returns null when nothing matches. Used to check current DB status
    before adding to a client-side pickup batch.
    """
    try:
        if not tracking_number:
            return None
        pkg = await db.packages.find_one(
            {'tracking_number': tracking_number},
            sort=[('timestamp', -1)]
        )
        if not pkg:
            return None
        return _to_response(pkg)
    except Exception as e:
        logger.error(f"Error in lookup: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/packages/batch-add", response_model=List[PackageResponse])
async def batch_add_packages(payload: BatchAddRequest):
    """Batch create multiple packages at once.
    Each item can carry tracking_number, recipient_name, phone_number, notes.
    Never resets or overwrites existing data — always inserts new records.
    """
    try:
        status = payload.status if payload.status in PACKAGE_STATUSES else "pending"
        if not payload.items:
            return []
        
        now = now_myt()
        date_str = now.strftime("%Y-%m-%d")
        docs = []
        for item in payload.items:
            # Skip items with no useful info
            if not any([item.tracking_number, item.recipient_name, item.phone_number, item.notes]):
                continue
            docs.append({
                'recipient_name': item.recipient_name,
                'phone_number': item.phone_number,
                'tracking_number': item.tracking_number,
                'notes': item.notes,
                'status': status,
                'timestamp': now,
                'date': date_str,
            })
        
        if not docs:
            return []
        
        result = await db.packages.insert_many(docs)
        # docs get _id populated after insert_many
        return [_to_response(d) for d in docs]
    except Exception as e:
        logger.error(f"Error in batch add: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/packages/batch-status", response_model=BatchStatusResponse)
async def batch_update_status(update: BatchStatusUpdate):
    """Batch update multiple packages' status.
    Accepts a list of items with either 'id' or 'tracking_number'.
    - Packages ALREADY in the target status are SKIPPED (no change, no timestamp update).
    - Existing packages in a different status are updated.
    - Items with unknown tracking numbers create a new package with the target status.
    Returns three lists: updated, skipped (already at target), and created.
    """
    try:
        if update.status not in PACKAGE_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {PACKAGE_STATUSES}")
        if not update.items:
            return BatchStatusResponse(updated=[], skipped=[], created=[])
        
        now = now_myt()
        set_payload = {'status': update.status, 'status_updated_at': now}
        updated: List[dict] = []
        skipped: List[dict] = []
        created: List[dict] = []
        
        for item in update.items:
            existing = None
            if item.id and ObjectId.is_valid(item.id):
                existing = await db.packages.find_one({'_id': ObjectId(item.id)})
            elif item.tracking_number:
                existing = await db.packages.find_one(
                    {'tracking_number': item.tracking_number},
                    sort=[('timestamp', -1)]
                )
            
            if existing:
                # Skip if already at target status — do NOT touch the document
                if existing.get('status') == update.status:
                    skipped.append(existing)
                    continue
                pkg = await db.packages.find_one_and_update(
                    {'_id': existing['_id']},
                    {'$set': set_payload},
                    return_document=True
                )
                if pkg:
                    updated.append(pkg)
            elif item.tracking_number:
                # Create a new package with this tracking number and the target status
                new_pkg = {
                    'tracking_number': item.tracking_number,
                    'recipient_name': None,
                    'phone_number': None,
                    'notes': None,
                    'status': update.status,
                    'timestamp': now,
                    'date': now.strftime("%Y-%m-%d"),
                    'status_updated_at': now,
                }
                ins = await db.packages.insert_one(new_pkg)
                new_pkg['_id'] = ins.inserted_id
                created.append(new_pkg)
        
        return BatchStatusResponse(
            updated=[_to_response(p) for p in updated],
            skipped=[_to_response(p) for p in skipped],
            created=[_to_response(p) for p in created],
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in batch status update: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.patch("/packages/by-tracking/status", response_model=PackageResponse)
async def update_package_status_by_tracking(
    tracking_number: str,
    update: StatusUpdate
):
    """Update the most recent package's status by tracking number.
    If not found, creates a new package with that tracking number and status."""
    try:
        if update.status not in PACKAGE_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {PACKAGE_STATUSES}")
        
        # Find most recent package with this tracking number
        existing = await db.packages.find_one(
            {'tracking_number': tracking_number},
            sort=[('timestamp', -1)]
        )
        
        if existing:
            # Update existing
            result = await db.packages.find_one_and_update(
                {'_id': existing['_id']},
                {'$set': {'status': update.status, 'status_updated_at': now_myt()}},
                return_document=True
            )
            return _to_response(result)
        else:
            # Create new package with the given tracking number and status
            now = now_myt()
            new_pkg = {
                'tracking_number': tracking_number,
                'recipient_name': None,
                'phone_number': None,
                'notes': None,
                'status': update.status,
                'timestamp': now,
                'date': now.strftime("%Y-%m-%d"),
            }
            result = await db.packages.insert_one(new_pkg)
            new_pkg['_id'] = result.inserted_id
            return _to_response(new_pkg)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating status by tracking: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.patch("/packages/{package_id}/status", response_model=PackageResponse)
async def update_package_status(package_id: str, update: StatusUpdate):
    """Update a package's status by ID"""
    try:
        if update.status not in PACKAGE_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {PACKAGE_STATUSES}")
        if not ObjectId.is_valid(package_id):
            raise HTTPException(status_code=404, detail="Package not found")
        
        result = await db.packages.find_one_and_update(
            {'_id': ObjectId(package_id)},
            {'$set': {'status': update.status, 'status_updated_at': now_myt()}},
            return_document=True
        )
        if not result:
            raise HTTPException(status_code=404, detail="Package not found")
        
        return _to_response(result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/goals", response_model=Goals)
async def get_goals():
    """Get user's daily goals"""
    try:
        goals = await db.settings.find_one({'type': 'goals'})
        if goals:
            return Goals(
                packages_goal=goals.get('packages_goal', 100),
                earnings_goal=goals.get('earnings_goal', 200.0)
            )
        return Goals()
    except Exception as e:
        logger.error(f"Error fetching goals: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.put("/goals", response_model=Goals)
async def update_goals(update: GoalsUpdate):
    """Update user's daily goals"""
    try:
        existing = await db.settings.find_one({'type': 'goals'}) or {}
        new_goals = {
            'type': 'goals',
            'packages_goal': update.packages_goal if update.packages_goal is not None else existing.get('packages_goal', 100),
            'earnings_goal': update.earnings_goal if update.earnings_goal is not None else existing.get('earnings_goal', 200.0),
            'updated_at': now_myt(),
        }
        await db.settings.update_one(
            {'type': 'goals'},
            {'$set': new_goals},
            upsert=True
        )
        return Goals(
            packages_goal=new_goals['packages_goal'],
            earnings_goal=new_goals['earnings_goal']
        )
    except Exception as e:
        logger.error(f"Error updating goals: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/packages/export")
async def export_packages(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
):
    """Export packages as CSV"""
    try:
        query = {}
        if start_date or end_date:
            date_filter = {}
            if start_date:
                date_filter['$gte'] = start_date
            if end_date:
                date_filter['$lte'] = end_date
            query['date'] = date_filter
        
        packages = await db.packages.find(query).sort('timestamp', -1).to_list(10000)
        
        # Create CSV
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Date', 'Time', 'Status', 'Recipient', 'Phone', 'Tracking Number', 'Notes'])
        
        for pkg in packages:
            writer.writerow([
                pkg['date'],
                pkg['timestamp'].strftime('%H:%M:%S'),
                pkg.get('status', 'pending'),
                pkg.get('recipient_name', ''),
                pkg.get('phone_number', ''),
                pkg.get('tracking_number', ''),
                pkg.get('notes', '')
            ])
        
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=packages_export.csv"}
        )
    except Exception as e:
        logger.error(f"Error exporting packages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class EmailReportRequest(BaseModel):
    recipient_email: EmailStr
    range: str = "all"  # "all", "week", "month"


@api_router.post("/reports/email")
async def send_email_report(request: EmailReportRequest):
    """Send a text-summary email report to the recipient via Emergent-managed Resend.
    Range can be 'all', 'week' (last 7 days), or 'month' (last 30 days).
    """
    if not EMERGENT_EMAIL_KEY:
        raise HTTPException(status_code=500, detail="Email service not configured")

    try:
        now = now_myt()
        today = today_myt_str()
        rng = (request.range or "all").lower()

        # Build query based on range
        query = {}
        if rng == "week":
            query['date'] = {'$gte': days_ago_myt_str(7)}
            range_label = "Last 7 Days"
            start_label = days_ago_myt_str(7)
        elif rng == "month":
            query['date'] = {'$gte': days_ago_myt_str(30)}
            range_label = "Last 30 Days"
            start_label = days_ago_myt_str(30)
        else:
            range_label = "All Time"
            start_label = None

        # Aggregate summary numbers
        total = await db.packages.count_documents(query)
        pending = await db.packages.count_documents({**query, 'status': 'pending'})
        picked_up = await db.packages.count_documents({**query, 'status': 'picked_up'})
        returned = await db.packages.count_documents({**query, 'status': 'returned'})

        # Earnings within the same range
        earnings_query = {}
        if rng == "week":
            earnings_query = {'date': {'$gte': days_ago_myt_str(7)}}
        elif rng == "month":
            earnings_query = {'date': {'$gte': days_ago_myt_str(30)}}
        earnings_docs = await db.earnings.find(earnings_query).to_list(1000)
        total_earnings = sum(e.get('amount', 0) for e in earnings_docs)

        # Daily breakdown (top 10 recent days within range)
        pipeline = [
            {'$match': query} if query else {'$match': {}},
            {'$group': {'_id': '$date', 'count': {'$sum': 1}}},
            {'$sort': {'_id': -1}},
            {'$limit': 10},
        ]
        daily = await db.packages.aggregate(pipeline).to_list(10)
        earnings_map = {e['date']: e.get('amount', 0) for e in earnings_docs}

        # Build HTML rows for daily breakdown
        daily_rows_html = ""
        if daily:
            for row in daily:
                d = row['_id']
                c = row['count']
                e = earnings_map.get(d, 0)
                daily_rows_html += (
                    f"<tr>"
                    f"<td style='padding:8px 12px;border-bottom:1px solid #E5E5EA;font-size:14px;color:#1D1D1F;'>{d}</td>"
                    f"<td style='padding:8px 12px;border-bottom:1px solid #E5E5EA;font-size:14px;color:#1D1D1F;text-align:right;'>{c}</td>"
                    f"<td style='padding:8px 12px;border-bottom:1px solid #E5E5EA;font-size:14px;color:#34C759;text-align:right;'>RM {e:.2f}</td>"
                    f"</tr>"
                )
        else:
            daily_rows_html = (
                "<tr><td colspan='3' style='padding:16px;text-align:center;color:#8E8E93;font-size:14px;'>"
                "No records in this range.</td></tr>"
            )

        generated_str = now.strftime("%Y-%m-%d %H:%M MYT")
        range_str = f"{start_label} → {today}" if start_label else "All available records"

        html = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F2F2F7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F2F2F7;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#FFFFFF;border-radius:16px;overflow:hidden;">
        <tr><td style="background:#007AFF;padding:24px;text-align:center;">
          <div style="color:#FFFFFF;font-size:14px;opacity:0.9;">Package Tracker Report</div>
          <div style="color:#FFFFFF;font-size:24px;font-weight:700;margin-top:4px;">{range_label}</div>
          <div style="color:#FFFFFF;font-size:12px;opacity:0.85;margin-top:8px;">{range_str}</div>
        </td></tr>

        <tr><td style="padding:24px;">
          <div style="font-size:14px;color:#8E8E93;margin-bottom:16px;">Summary</div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
            <tr>
              <td style="padding:12px;background:#F2F2F7;border-radius:8px;text-align:center;width:48%;">
                <div style="font-size:12px;color:#8E8E93;">Total Packages</div>
                <div style="font-size:28px;font-weight:700;color:#1D1D1F;margin-top:4px;">{total}</div>
              </td>
              <td style="width:4%;"></td>
              <td style="padding:12px;background:#F2F2F7;border-radius:8px;text-align:center;width:48%;">
                <div style="font-size:12px;color:#8E8E93;">Total Earnings</div>
                <div style="font-size:28px;font-weight:700;color:#34C759;margin-top:4px;">RM {total_earnings:.2f}</div>
              </td>
            </tr>
          </table>

          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
            <tr>
              <td style="padding:12px;background:#FFF4E5;border-radius:8px;text-align:center;width:32%;">
                <div style="font-size:11px;color:#FF9500;font-weight:600;">PENDING</div>
                <div style="font-size:20px;font-weight:700;color:#1D1D1F;margin-top:4px;">{pending}</div>
              </td>
              <td style="width:2%;"></td>
              <td style="padding:12px;background:#E8F8ED;border-radius:8px;text-align:center;width:32%;">
                <div style="font-size:11px;color:#34C759;font-weight:600;">PICKED UP</div>
                <div style="font-size:20px;font-weight:700;color:#1D1D1F;margin-top:4px;">{picked_up}</div>
              </td>
              <td style="width:2%;"></td>
              <td style="padding:12px;background:#FDECEA;border-radius:8px;text-align:center;width:32%;">
                <div style="font-size:11px;color:#FF3B30;font-weight:600;">RETURNED</div>
                <div style="font-size:20px;font-weight:700;color:#1D1D1F;margin-top:4px;">{returned}</div>
              </td>
            </tr>
          </table>

          <div style="font-size:14px;color:#8E8E93;margin-bottom:8px;">Daily Breakdown (most recent)</div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E5EA;border-radius:8px;overflow:hidden;">
            <tr style="background:#F2F2F7;">
              <th style="padding:10px 12px;text-align:left;font-size:12px;color:#8E8E93;">Date</th>
              <th style="padding:10px 12px;text-align:right;font-size:12px;color:#8E8E93;">Packages</th>
              <th style="padding:10px 12px;text-align:right;font-size:12px;color:#8E8E93;">Earnings</th>
            </tr>
            {daily_rows_html}
          </table>

          <div style="margin-top:24px;padding-top:16px;border-top:1px solid #E5E5EA;font-size:12px;color:#8E8E93;text-align:center;">
            Generated at {generated_str}<br/>
            All times in Malaysia Time (MYT, UTC+8)
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""

        subject = f"Package Tracker Report — {range_label} ({today})"
        payload = {
            "to": [request.recipient_email],
            "subject": subject,
            "html": html,
            "from_name": EMAIL_FROM_NAME,
        }

        async with httpx.AsyncClient(timeout=30) as http_client:
            resp = await http_client.post(
                f"{EMAIL_BASE_URL}/api/v1/email/send",
                headers={"X-Email-Key": EMERGENT_EMAIL_KEY},
                json=payload,
            )
        if resp.status_code >= 400:
            logger.error(f"Email send failed: {resp.status_code} {resp.text}")
            raise HTTPException(status_code=502, detail="Failed to send email")

        return {
            "status": "success",
            "message": f"Report sent to {request.recipient_email}",
            "summary": {
                "range": range_label,
                "total": total,
                "pending": pending,
                "picked_up": picked_up,
                "returned": returned,
                "earnings": round(total_earnings, 2),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending email report: {e}")
        raise HTTPException(status_code=500, detail="Failed to send email report")


@api_router.delete("/packages/{package_id}")
async def delete_package(package_id: str):
    """Delete a package"""
    try:
        if not ObjectId.is_valid(package_id):
            raise HTTPException(status_code=404, detail="Package not found")
            
        result = await db.packages.delete_one({'_id': ObjectId(package_id)})
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Package not found")
        return {"message": "Package deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting package: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
