"""Mirror PlannedMeals onto a shared Google Calendar via a service account."""
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.urls import reverse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from meals.models import HouseholdSettings, PlannedMeal

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar.events']
EVENT_ICONS = {'breakfast': '🍳', 'lunch': '🥪', 'dinner': '🍽'}


def service_account_email() -> str:
    """The address a calendar has to be shared with, or '' when the key file is missing."""
    try:
        with open(settings.GOOGLE_SERVICE_ACCOUNT_FILE) as f:
            return json.load(f).get('client_email', '')
    except (OSError, ValueError):
        return ''


def check_access(calendar_id: str) -> str | None:
    """None when the service account can read events on `calendar_id`, else why it can't."""
    try:
        _service().events().list(calendarId=calendar_id, maxResults=1).execute()
    except HttpError as e:
        if e.resp.status in (403, 404):
            return "The bot can't see that calendar. Check the ID, and that it's shared with the bot."
        return f'Google Calendar returned an error ({e.resp.status}).'
    except OSError:
        return 'The service account key file is missing on the server.'
    return None


def _service():
    credentials = service_account.Credentials.from_service_account_file(
        settings.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build('calendar', 'v3', credentials=credentials, cache_discovery=False)


def _event_content(meal: PlannedMeal) -> dict:
    """What the app owns on an event: the title, description and link back. Never the time."""
    recipe = meal.recipe
    ingredients = '\n'.join(f"• {i['quantity']} {i['name']}" for i in recipe.ingredients)
    link = settings.APP_BASE_URL + reverse('meal_detail', args=[meal.pk])
    description = (
        f"{recipe.summary}\n\n"
        f"⏱ {recipe.total_minutes or '?'} min\n\n"
        f"Ingredients:\n{ingredients}\n\n"
        f"Instructions:\n{recipe.instructions}\n\n"
        f"Rate or replace: {link}"
    )
    return {
        'summary': f'{EVENT_ICONS[meal.meal_type]} {recipe.name}',
        'description': description,
        'source': {'title': 'Meal Planner', 'url': link},
    }


def sync_meal(meal: PlannedMeal):
    """Create or update the meal's calendar event. No-op when no calendar is configured.

    The time is set only when the event is created. Later syncs patch just the content, so a
    meal moved (or given guests, reminders, a colour) in Google Calendar stays that way.
    """
    household = HouseholdSettings.load()
    if not household.calendar_id:
        return
    events = _service().events()
    content = _event_content(meal)
    if meal.calendar_event_id:
        try:
            events.patch(calendarId=household.calendar_id, eventId=meal.calendar_event_id, body=content).execute()
            return
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise
            logger.info('Calendar event %s gone; recreating', meal.calendar_event_id)
    start_time, minutes = household.meal_window(meal.meal_type)
    start = datetime.combine(meal.date, start_time)
    end = start + timedelta(minutes=minutes)
    body = {
        **content,
        'start': {'dateTime': start.isoformat(), 'timeZone': settings.TIME_ZONE},
        'end': {'dateTime': end.isoformat(), 'timeZone': settings.TIME_ZONE},
    }
    created = events.insert(calendarId=household.calendar_id, body=body).execute()
    meal.calendar_event_id = created['id']
    meal.save(update_fields=['calendar_event_id'])


def _shift(when: dict, days: int) -> dict:
    """An event start/end moved by `days` on the calendar, keeping its local time across DST changes."""
    if 'date' in when:  # all-day event
        return {'date': (date.fromisoformat(when['date']) + timedelta(days=days)).isoformat()}
    zone = when.get('timeZone') or settings.TIME_ZONE
    local = datetime.fromisoformat(when['dateTime']).astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    return {'dateTime': (local + timedelta(days=days)).isoformat(), 'timeZone': zone}


def move_meal_event(meal: PlannedMeal, days: int):
    """Move the meal's event `days` days (after its date changed), keeping the time and length it has
    in Google Calendar. An event that's missing is recreated at the household's default time."""
    household = HouseholdSettings.load()
    if not household.calendar_id:
        return
    if meal.calendar_event_id:
        events = _service().events()
        try:
            event = events.get(calendarId=household.calendar_id, eventId=meal.calendar_event_id).execute()
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise
            logger.info('Calendar event %s gone; recreating', meal.calendar_event_id)
        else:
            body = {**_event_content(meal), 'start': _shift(event['start'], days), 'end': _shift(event['end'], days)}
            events.patch(calendarId=household.calendar_id, eventId=meal.calendar_event_id, body=body).execute()
            return
        meal.calendar_event_id = ''
    sync_meal(meal)


def delete_meal_event(meal: PlannedMeal):
    household = HouseholdSettings.load()
    if not (household.calendar_id and meal.calendar_event_id):
        return
    try:
        _service().events().delete(calendarId=household.calendar_id, eventId=meal.calendar_event_id).execute()
    except HttpError as e:
        if e.resp.status not in (404, 410):
            raise
