from django.contrib import admin
from django.urls import include, path

from meals import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("", views.Index.as_view(), name="index"),
    path("logout", views.Logout.as_view(), name="logout"),
    path("plan/<str:week_start>/", views.PlanView.as_view(), name="plan"),
    path("plan/<str:week_start>/generate/", views.GeneratePlan.as_view(), name="generate_plan"),
    path("plan/<str:week_start>/email/", views.EmailPlan.as_view(), name="email_plan"),
    path("plan/<str:week_start>/groceries/", views.Groceries.as_view(), name="groceries"),
    path("groceries/", views.CurrentGroceries.as_view(), name="current_groceries"),
    path("meals/<int:pk>/", views.MealDetail.as_view(), name="meal_detail"),
    path("meals/<int:pk>/replace/", views.ReplaceMeal.as_view(), name="replace_meal"),
    path("meals/<int:pk>/lock/", views.ToggleLock.as_view(), name="toggle_lock"),
    path("recipes/", views.RecipeList.as_view(), name="recipes"),
    path("recipes/<int:pk>/", views.RecipeDetail.as_view(), name="recipe_detail"),
    path("recipes/<int:pk>/rate/", views.RateRecipe.as_view(), name="rate_recipe"),
    path("recipes/<int:pk>/archive/", views.ToggleArchive.as_view(), name="toggle_archive"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("settings/store/", views.ChooseStore.as_view(), name="choose_store"),
    path("settings/calendar/", views.SetCalendar.as_view(), name="set_calendar"),
    path("settings/calendar/sync/", views.SyncCalendar.as_view(), name="sync_calendar"),
    path("kroger/connect/", views.KrogerConnect.as_view(), name="kroger_connect"),
    path("kroger/callback/", views.KrogerCallback.as_view(), name="kroger_callback"),
    path("internal/generate-week/", views.InternalGenerateWeek.as_view(), name="internal_generate_week"),
]
