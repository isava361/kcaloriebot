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
			proteinNull := sql.NullFloat64{Float64: protein, Valid: true}
			setUserProtein(userID, proteinNull, db)
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
			fatNull := sql.NullFloat64{Float64: fat, Valid: true}
			setUserFat(userID, fatNull, db)
            msg := tgbotapi.NewMessage(message.Chat.ID, "Enter the carbs per 100g (or send Skip to omit):")
			msg.ReplyMarkup = skipkeyboard
            bot.Send(msg)
        }

    case stateWaitingForCarbs:
		// Process carbs input or skip
		if message.Text == "Skip" {
			calories, grams, protein, fat, _ := getUserFoodEntry(userID, db)
			carbsNull := sql.NullFloat64{Valid: false}
			err := addFood(userID, calories, grams, protein, fat, carbsNull, db)
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
			calories, grams, protein, fat, _ := getUserFoodEntry(userID, db)
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
        } else if message.Text == "Delete Food" {
			// Retrieve today's food entries for the user
			entries, err := getTodayFoodEntries(userID, db)
			if err != nil {
				msg := tgbotapi.NewMessage(message.Chat.ID, "Failed to retrieve today's food entries. Please try again.")
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
			msg := tgbotapi.NewMessage(message.Chat.ID, "Select a food entry to delete:")
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
            tgbotapi.NewKeyboardButton("Delete Food"),
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