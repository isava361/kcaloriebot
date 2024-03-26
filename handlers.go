package main

import (
	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"database/sql"
	_ "github.com/mattn/go-sqlite3"
	"fmt"
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

func handleMessage(bot *tgbotapi.BotAPI, message *tgbotapi.Message, db *sql.DB) error {
    userID := message.From.ID

	skipkeyboard := tgbotapi.NewReplyKeyboard(
		tgbotapi.NewKeyboardButtonRow(
			tgbotapi.NewKeyboardButton("Skip"),
		),
	)

    // Check user's current state and respond accordingly
    switch getUserState(userID, db) {
    case stateWaitingForCalories:
        // Process calories input
        calories, err := strconv.ParseFloat(message.Text, 64)
        if err != nil {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid calories value. Please enter a valid number.")
            bot.Send(msg)
            return nil
        }
        setUserState(userID, stateWaitingForGrams, db)
        setUserCalories(userID, calories, db)
        msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the grams of food:")
        bot.Send(msg)

    case stateWaitingForGrams:
        // Process grams input
        grams, err := strconv.ParseFloat(message.Text, 64)
        if err != nil {
            msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid grams value. Please enter a valid number.")
			msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
            return nil
        }
        setUserState(userID, stateWaitingForProtein, db)
        setUserGrams(userID, grams, db)
        msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the protein per 100g (or send Skip to omit):")
		msg.ReplyMarkup = skipkeyboard
        bot.Send(msg)

    case stateWaitingForProtein:
        // Process protein input or skip
        if message.Text == "Skip" {
			proteinNull := sql.NullFloat64{Valid: false}
			setUserProtein(userID, proteinNull, db)
			setUserState(userID, stateWaitingForFat, db)
			msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the fat per 100g (or send Skip to omit):")
			msg.ReplyMarkup = skipkeyboard
			bot.Send(msg)
        } else {
            protein, err := strconv.ParseFloat(message.Text, 64)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid protein value. Please enter a valid number or send Skip to omit.")
				msg.ReplyMarkup = skipkeyboard
                bot.Send(msg)
                return nil
            }
            setUserState(userID, stateWaitingForFat, db)
            setUserProtein(userID, protein, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the fat per 100g (or send Skip to omit):")
			msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        }

    case stateWaitingForFat:
        // Process fat input or skip
        if message.Text == "Skip" {
			fatNull := sql.NullFloat64{Valid: false}
			setUserFat(userID, fatNull, db)
			setUserState(userID, stateWaitingForCarbs, db)
			msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the carbs per 100g (or send Skip to omit):")
			msg.ReplyMarkup = skipkeyboard
			bot.Send(msg)
        } else {
            fat, err := strconv.ParseFloat(message.Text, 64)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid fat value. Please enter a valid number or send Skip to omit.")
                bot.Send(msg)
                return nil
            }
            setUserState(userID, stateWaitingForCarbs, db)
            setUserFat(userID, fat, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the carbs per 100g (or send Skip to omit):")
			msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        }

    case stateWaitingForCarbs:
        // Process carbs input or skip
        if message.Text == "Skip" {
            calories, grams, protein, fat, carbs := getUserFoodEntry(userID, db)
            err := addFood(userID, calories, grams, protein, fat, carbs, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to add food entry. Please try again.")
                bot.Send(msg)
                return nil
            }
            setUserState(userID, stateDefault, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry added successfully!")
            bot.Send(msg)
            sendDefaultKeyboard(bot, message.Chat.ID)
        } else {
            carbs, err := strconv.ParseFloat(message.Text, 64)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Invalid carbs value. Please enter a valid number or send Skip to omit.")
				msg.ReplyMarkup = skipkeyboard
                bot.Send(msg)
                return nil
            }
            carbsNull := sql.NullFloat64{Float64: carbs, Valid: true}
			setUserCarbs(userID, carbsNull, db)
            calories, grams, protein, fat, carbs := getUserFoodEntry(userID, db)
            err = addFood(userID, calories, grams, protein, fat, carbsNull, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to add food entry. Please try again.")
                bot.Send(msg)
                return nil
            }
            setUserState(userID, stateDefault, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Food entry added successfully!")
            bot.Send(msg)
            sendDefaultKeyboard(bot, message.Chat.ID)
        }

    default:
        // Handle callback queries
        if message.Text == "/start" {
            setUserState(userID, stateDefault, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Welcome to the Calorie Calculator Bot!")
            bot.Send(msg)
            sendDefaultKeyboard(bot, message.Chat.ID)
        } else if message.Text == "Add Food" {
            setUserState(userID, stateWaitingForCalories, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the calories per 100g:")
            bot.Send(msg)
        } else if message.Text == "Today Stats" {
            calories, protein, fat, carbs, err := getTodayStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to retrieve today's stats. Please try again.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Today's Stats:\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Yesterday Stats" {
            calories, protein, fat, carbs, err := getYesterdayStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to retrieve yesterday's stats. Please try again.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Yesterday's Stats:\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Week Stats" {
            calories, protein, fat, carbs, err := getWeekStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to retrieve week's stats. Please try again.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Week's Stats (Average):\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
            bot.Send(msg)
        } else if message.Text == "Month Stats" {
            calories, protein, fat, carbs, err := getMonthStats(userID, db)
            if err != nil {
                msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to retrieve month's stats. Please try again.")
                bot.Send(msg)
                return nil
            }
            msgText := fmt.Sprintf("Month's Stats (Average):\nCalories: %.2f\nProtein: %.2f\nFat: %.2f\nCarbs: %.2f", calories, protein.Float64, fat.Float64, carbs.Float64)
            msg := tgbotapi.NewMessage(message.Chat.ID, msgText)
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
            tgbotapi.NewKeyboardButton("Today Stats"),
        ),
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Yesterday Stats"),
            tgbotapi.NewKeyboardButton("Week Stats"),
        ),
        tgbotapi.NewKeyboardButtonRow(
            tgbotapi.NewKeyboardButton("Month Stats"),
        ),
    )
    msg := tgbotapi.NewMessage(chatID, "Select an option:")
    msg.ReplyMarkup = keyboard
    bot.Send(msg)
}