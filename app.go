package main

import (
	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"log"
	"database/sql"
	_ "github.com/mattn/go-sqlite3"
	"strings"
	"strconv"
)

func main() {

	token, err := readBotToken("./config/token.txt")
	if err != nil {
		log.Panicf("Token error: ", err)
	}
	bot, err := tgbotapi.NewBotAPI(token)
	if err != nil {
		log.Panic(err)
	}

	log.Printf("Authorized on account %s", bot.Self.UserName)
	
	db, err := sql.Open("sqlite3", "./mydb.db")
	if err != nil {
		log.Println(err)
		return
	}
	defer db.Close()

	err = createTables(db)
	if err != nil {
		log.Fatalf("Failed to create tables: %v", err)
	}

	u := tgbotapi.NewUpdate(0)
	u.Timeout = 60

	updates := bot.GetUpdatesChan(u)
	

	for update := range updates {
        if update.Message != nil {
            m := update.Message
            if m.From == nil {
                continue
            }

            log.Printf("Message received")
            err := handleMessage(bot, m, db)
            if err != nil {
                log.Printf("[%s] %s, err: %s", update.Message.From.UserName, update.Message.Text, err.Error())
                continue
            }

            log.Printf("[%s] %s", update.Message.From.UserName, update.Message.Text)
        } else if update.CallbackQuery != nil {
			userID := update.CallbackQuery.From.ID
		
			if strings.HasPrefix(update.CallbackQuery.Data, "delete_") {
				err := handleDeleteFoodEntryCallback(bot, update.CallbackQuery, db)
				if err != nil {
					log.Printf("Error handling delete food entry callback: %s", err)
				}
			} else {
				log.Printf("Unhandled callback data: %s", update.CallbackQuery.Data)
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Unhandled callback data")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
				}
			}
		}
		


func createTables(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS users (
			user_id INTEGER PRIMARY KEY,
			state INTEGER NOT NULL
		);

		CREATE TABLE IF NOT EXISTS food_entries (
			entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
			user_id INTEGER NOT NULL,
			entry_date DATE NOT NULL,
			calories REAL,
			grams REAL,
			protein REAL,
			fat REAL,
			carbs REAL,
			FOREIGN KEY (user_id) REFERENCES users (user_id)
		);
	`)
	if err != nil {
		return err
	}
	return nil
}

func handleDeleteFoodEntryCallback(bot *tgbotapi.BotAPI, callbackQuery *tgbotapi.CallbackQuery, db *sql.DB) error {
    // Create the CallbackConfig
    callbackConfig := tgbotapi.NewCallback(callbackQuery.ID, "")

    // Send the acknowledgment
    if _, err := bot.Request(callbackConfig); err != nil {
        log.Printf("Error answering callback query: %s", err)
        return err
    }

    userID := callbackQuery.From.ID
    chatID := callbackQuery.Message.Chat.ID
    data := callbackQuery.Data
    messageID := callbackQuery.Message.MessageID

    if strings.HasPrefix(data, "delete_") {
        entryID, err := strconv.ParseInt(strings.TrimPrefix(data, "delete_"), 10, 64)
        if err != nil {
            log.Printf("Invalid food entry ID: %s", err)
            return err
        }

        err = deleteFoodEntry(entryID, db)
        if err != nil {
            log.Printf("Failed to delete food entry: %s", err)
            return err
        }

        // Send a confirmation message
        msg := tgbotapi.NewMessage(chatID, "Food entry deleted successfully!")
        bot.Send(msg)

        // Retrieve today's food entries for the user
        entries, err := getTodayFoodEntries(userID, db)
        if err != nil {
            log.Printf("Failed to retrieve today's food entries: %s", err)
            return err
        }

        if len(entries) == 0 {
            msg := tgbotapi.NewMessage(chatID, "No food entries found for today.")
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
        msg := tgbotapi.NewMessage(chatID, "Select a food entry to delete:")
        msg.ReplyMarkup = keyboard
        bot.Send(msg)
    }

    return nil
}