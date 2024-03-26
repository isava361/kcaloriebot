package main

import (
	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"database/sql"
	_ "github.com/mattn/go-sqlite3"
	"fmt"
	"strings"
	"strconv"
) 

const (
    stateDefault = iota
    stateWaitingForCalories
    stateWaitingForGrams
    stateWaitingForProtein
    stateWaitingForFat
    stateWaitingForCarbs
)

type UserInput struct {
    Calories float64
    Grams    float64
    Protein  sql.NullFloat64
    Fat      sql.NullFloat64
}

var userInputs = make(map[int64]*UserInput)

func handleMessage(bot *tgbotapi.BotAPI, message *tgbotapi.Message, db *sql.DB) error {
    userID := message.From.ID

    skipkeyboard := tgbotapi.NewReplyKeyboard(
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Skip"),
            tgbotapi.NewKeyboardButton("Cancel"),
        ),
    )

	cancelkeyboard := tgbotapi.NewReplyKeyboard(
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Cancel"),
        ),
    )

    defaultkeyboard := tgbotapi.NewReplyKeyboard(
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Add Food"),
            tgbotapi.NewKeyboardButton("Food Today"),
        ),
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Today Stats"),
            tgbotapi.NewKeyboardButton("Yesterday Stats"),
        ),
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Week Stats"),
			tgbotapi.NewKeyboardButton("Month Stats"),
        ),
    )

    input, ok := userInputs[userID]
    if !ok {
        input = &UserInput{}
        userInputs[userID] = input
    }

    // Check user's current state and respond accordingly
    switch getUserState(userID, db) {
    case stateWaitingForCalories:
        // Process calories input
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }

        calories, err := strconv.ParseFloat(message.Text, 64)
        if err != nil {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid calories value. Please enter a valid number or send Cancel to cancel the food entry.")
            msg.ReplyMarkup = cancelkeyboard
            bot.Send(msg)
            return nil
        }
        input.Calories = calories
        setUserState(userID, stateWaitingForGrams, db)
        msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the grams of food:")
        msg.ReplyMarkup = cancelkeyboard
        bot.Send(msg)

    case stateWaitingForGrams:
        // Process grams input
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }

        grams, err := strconv.ParseFloat(message.Text, 64)
        if err != nil {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid grams value. Please enter a valid number or send Cancel to cancel the food entry.")
			msg.ReplyMarkup = cancelkeyboard
            bot.Send(msg)
            return nil
        }
        input.Grams = grams
        setUserState(userID, stateWaitingForProtein, db)
        msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the protein per 100g (or send Skip to omit):")
        msg.ReplyMarkup = skipkeyboard
        bot.Send(msg)

    case stateWaitingForProtein:
        // Process protein input or skip
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }

        if message.Text == "Skip" {
            input.Protein = sql.NullFloat64{Valid: false}
            setUserState(userID, stateWaitingForFat, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the fat per 100g (or send Skip to omit):")
            msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        } else {
            protein, err := strconv.ParseFloat(message.Text, 64)
            if err != nil || protein > 100 || protein < -100 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid protein value. Please enter a valid number or send Skip to omit.")
                msg.ReplyMarkup = skipkeyboard
                bot.Send(msg)
                return nil
            }
            input.Protein = sql.NullFloat64{Float64: protein * input.Grams / 100, Valid: true}
            setUserState(userID, stateWaitingForFat, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the fat per 100g (or send Skip to omit):")
            msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        }

    case stateWaitingForFat:
        // Process fat input or skip
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }

        if message.Text == "Skip" {
            input.Fat = sql.NullFloat64{Valid: false}
            setUserState(userID, stateWaitingForCarbs, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the carbs per 100g (or send Skip to omit):")
            msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        } else {
            fat, err := strconv.ParseFloat(message.Text, 64)
            if err != nil || fat > 100 || fat < -100 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid fat value. Please enter a valid number or send Skip to omit.")
                bot.Send(msg)
                return nil
            }
            input.Fat = sql.NullFloat64{Float64: fat * input.Grams / 100, Valid: true}
            setUserState(userID, stateWaitingForCarbs, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the carbs per 100g (or send Skip to omit):")
            msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        }

    case stateWaitingForCarbs:
        // Process carbs input or skip
        if message.Text == "Cancel" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry canceled.")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
            return nil
        }

        if message.Text == "Skip" {
            calories := input.Calories
            grams := input.Grams
            protein := input.Protein
            fat := input.Fat
            carbsNull := sql.NullFloat64{Valid: false}
            err := addFood(userID, calories*grams/100, grams, protein, fat, carbsNull, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to add food entry. Please try again.")
                bot.Send(msg)
                return nil
            }
            delete(userInputs, userID)
            setUserState(userID, stateDefault, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry added successfully!")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
        } else {
            carbs, err := strconv.ParseFloat(message.Text, 64)
            if err != nil || carbs > 100 || carbs < -100 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid carbs value. Please enter a valid number or send Skip to omit.")
                msg.ReplyMarkup = skipkeyboard
                bot.Send(msg)
                return nil
            }
            calories := input.Calories
            grams := input.Grams
            protein := input.Protein
            fat := input.Fat
            if protein.Valid && fat.Valid && carbs > 100 {
                if (protein.Float64+fat.Float64+carbs) > 100 {
                    msg := tgbotapi.NewMessage(message.Chat.ID, "Your values for macronutrients add up to more than 100g. Please start again")
                    msg.ReplyMarkup = defaultkeyboard
                    bot.Send(msg)
                    delete(userInputs, userID)
                    setUserState(userID, stateDefault, db)
                    return nil
                }
            }
            carbsNull := sql.NullFloat64{Float64: carbs * input.Grams / 100, Valid: true}
            err = addFood(userID, calories*grams/100, grams, protein, fat, carbsNull, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to add food entry. Please try again.")
                bot.Send(msg)
                return nil
            }
            delete(userInputs, userID)
            setUserState(userID, stateDefault, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry added successfully!")
            msg.ReplyMarkup = defaultkeyboard
            bot.Send(msg)
        }

    default:
        // Handle callback queries
        if message.Text == "/start" {
            setUserState(userID, stateDefault, db)
            delete(userInputs, userID)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Welcome to the Calorie Calculator Bot!")
            bot.Send(msg)
            sendDefaultKeyboard(bot, message.Chat.ID)
        } else if message.Text == "Add Food" {
            setUserState(userID, stateWaitingForCalories, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the calories per 100g:")
            msg.ReplyMarkup = cancelkeyboard
            bot.Send(msg)
        } else if message.Text == "Today Stats" {
            calories, protein, fat, carbs, err := getTodayStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "No food entries found for today.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Today's Stats:\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Yesterday Stats" {
            calories, protein, fat, carbs, err := getYesterdayStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "No food entries found for yesterday.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Yesterday's Stats:\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Week Stats" {
            calories, protein, fat, carbs, err := getWeekStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "No food entries found for the week.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Week's Stats (Average):\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Month Stats" {
            calories, protein, fat, carbs, err := getMonthStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "No food entries found for the month.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Month's Stats (Average):\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Food Today" {
            // Retrieve today's food entries for the user
            entries, err := getTodayFoodEntries(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "No food entries found for today.")
                bot.Send(msg)
                return nil
            }

            if len(entries) == 0 {
                msg := tgbotapi.NewMessage(message.Chat.ID, "No food entries found for today.")
                bot.Send(msg)
                return nil
            }

            // Create inline keyboard with food entry options
            var rows [][]tgbotapi.InlineKeyboardButton
            for _, entry := range entries {
                buttonText := fmt.Sprintf("Calories: %.2f, Grams: %.2f", entry.Calories, entry.Grams)
                button := tgbotapi.NewInlineKeyboardButtonData(buttonText, fmt.Sprintf("delete_%d", entry.EntryID))
                row := []tgbotapi.InlineKeyboardButton{button}
                rows = append(rows, row)
            }

            keyboard := tgbotapi.NewInlineKeyboardMarkup(rows...)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food added today. Press on a button to delete this entry:")
            msg.ReplyMarkup = keyboard
            bot.Send(msg)
        } else if strings.HasPrefix(message.Text, "delete_") {
            entryID, err := strconv.ParseInt(strings.TrimPrefix(message.Text, "delete_"), 10, 64)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid food entry ID.")
                bot.Send(msg)
                return nil
            }

            err = deleteFoodEntry(entryID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to delete food entry. Please try again.")
                bot.Send(msg)
                return nil
            }

            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry deleted successfully!")
            bot.Send(msg)
        } else {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid command. Please select an option from the keyboard.")
            bot.Send(msg)
        }
    }

    return nil
}

func sendDefaultKeyboard(bot *tgbotapi.BotAPI, chatID int64) {
    keyboard := tgbotapi.NewReplyKeyboard(
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Add Food"),
            tgbotapi.NewKeyboardButton("Food Today"),
        ),
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Today Stats"),
            tgbotapi.NewKeyboardButton("Yesterday Stats"),
        ),
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Week Stats"),
			tgbotapi.NewKeyboardButton("Month Stats"),
        ),
    )
    msg := tgbotapi.NewMessage(chatID, "Select an option:")
    msg.ReplyMarkup = keyboard
    bot.Send(msg)
}