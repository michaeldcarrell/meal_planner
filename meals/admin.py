from django.contrib import admin

from meals.models import (
    AllowedUser, CartPush, GroceryProduct, HouseholdSettings, KrogerAuth, MealPlan, MealSwap, PlannedMeal, Rating,
    Recipe,
)


@admin.register(AllowedUser)
class AllowedUserAdmin(admin.ModelAdmin):
    list_display = ('email', 'name', 'receives_plan_email')
    list_editable = ('receives_plan_email',)


@admin.register(HouseholdSettings)
class HouseholdSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not HouseholdSettings.objects.exists()


class RatingInline(admin.TabularInline):
    model = Rating
    extra = 0


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ('name', 'cuisine', 'total_minutes', 'archived', 'created_at')
    list_filter = ('archived', 'cuisine')
    search_fields = ('name',)
    inlines = [RatingInline]


class PlannedMealInline(admin.TabularInline):
    model = PlannedMeal
    extra = 0
    raw_id_fields = ('recipe',)


@admin.register(MealPlan)
class MealPlanAdmin(admin.ModelAdmin):
    list_display = ('week_start', 'created_at', 'emailed_at')
    inlines = [PlannedMealInline]


@admin.register(GroceryProduct)
class GroceryProductAdmin(admin.ModelAdmin):
    list_display = ('grocery_item', 'upc', 'description')
    search_fields = ('grocery_item', 'description')


admin.site.register(MealSwap)
admin.site.register(CartPush)
admin.site.register(KrogerAuth)
