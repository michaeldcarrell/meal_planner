from django.contrib import admin

from meals.models import (
    AllowedUser, CartPush, GroceryProduct, Household, HouseholdSettings, KrogerAuth, MealPlan, MealSwap, Membership,
    MemberPreferences, PlannedMeal, Rating, Recipe,
)


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 1
    fields = ('household', 'member', 'receives_plan_email')
    autocomplete_fields = ('member',)


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    list_display = ('name', 'member_list', 'created_at')
    search_fields = ('name',)
    inlines = [MembershipInline]

    @admin.display(description='Members')
    def member_list(self, obj):
        return ', '.join(str(m) for m in obj.members.all())


@admin.register(AllowedUser)
class AllowedUserAdmin(admin.ModelAdmin):
    list_display = ('email', 'name', 'active_household')
    search_fields = ('email', 'name')
    inlines = [MembershipInline]


@admin.register(HouseholdSettings)
class HouseholdSettingsAdmin(admin.ModelAdmin):
    list_display = ('household',)


class RatingInline(admin.TabularInline):
    model = Rating
    extra = 0


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ('name', 'household', 'cuisine', 'total_minutes', 'archived', 'created_at')
    list_filter = ('household', 'archived', 'cuisine')
    search_fields = ('name',)
    inlines = [RatingInline]


class PlannedMealInline(admin.TabularInline):
    model = PlannedMeal
    extra = 0
    raw_id_fields = ('recipe',)


@admin.register(MealPlan)
class MealPlanAdmin(admin.ModelAdmin):
    list_display = ('week_start', 'household', 'created_at', 'emailed_at')
    list_filter = ('household',)
    inlines = [PlannedMealInline]


@admin.register(GroceryProduct)
class GroceryProductAdmin(admin.ModelAdmin):
    list_display = ('grocery_item', 'household', 'upc', 'description')
    list_filter = ('household',)
    search_fields = ('grocery_item', 'description')


@admin.register(KrogerAuth)
class KrogerAuthAdmin(admin.ModelAdmin):
    list_display = ('household', 'updated_at')


admin.site.register(MealSwap)
admin.site.register(CartPush)
admin.site.register(MemberPreferences)
