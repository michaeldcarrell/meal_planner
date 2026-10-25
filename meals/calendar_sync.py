"""Mirror PlannedMeals onto a shared Google Calendar via a service account."""
import json
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.urls import reverse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from meals.models import HouseholdSettings, PlannedMeal

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar.events']


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


def _event_body(meal: PlannedMeal, household: HouseholdSettings) -> dict:
    recipe = meal.recipe
    start = datetime.combine(meal.date, household.dinner_time)
    end = start + timedelta(minutes=household.dinner_minutes)
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
        'summary': f'🍽 {recipe.name}',
        'description': description,
        'start': {'dateTime': start.isoformat(), 'timeZone': settings.TIME_ZONE},
        'end': {'dateTime': end.isoformat(), 'timeZone': settings.TIME_ZONE},
        'source': {'title': 'Meal Planner', 'url': link},
    }


def sync_meal(meal: PlannedMeal):
    """Create or update the meal's calendar event. No-op when no calendar is configured."""
    household = HouseholdSettings.load()
    if not household.calendar_id:
        return
    events = _service().events()
    body = _event_body(meal, household)
    if meal.calendar_event_id:
        try:
            events.update(calendarId=household.calendar_id, eventId=meal.calendar_event_id, body=body).execute()
            return
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise
            logger.info('Calendar event %s gone; recreating', meal.calendar_event_id)
    created = events.insert(calendarId=household.calendar_id, body=body).execute()
    meal.calendar_event_id = created['id']
    meal.save(update_fields=['calendar_event_id'])


def delete_meal_event(meal: PlannedMeal):
    household = HouseholdSettings.load()
    if not (household.calendar_id and meal.calendar_event_id):
        return
    try:
        _service().events().delete(calendarId=household.calendar_id, eventId=meal.calendar_event_id).execute()
    except HttpError as e:
        if e.resp.status not in (404, 410):
            raise
