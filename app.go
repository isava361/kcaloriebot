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
            // Check if the callback data starts with "delete_"
            if strings.HasPrefix(update.CallbackQuery.Data, "delete_") {
                entryID, err := strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "delete_"), 10, 64)
                if err != nil {
                    log.Printf("Invalid food entry ID: %s", err)
                    callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid food entry ID")
                    if _, err := bot.Request(callbackConfig); err != nil {
                        log.Printf("Error sending callback response: %s", err)
                    }
                    continue
                }

                err = deleteFoodEntry(entryID, db)
                if err != nil {
                    log.Printf("Failed to delete food entry: %s", err)
                    callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Failed to delete food entry")
                    if _, err := bot.Request(callbackConfig); err != nil {
                        log.Printf("Error sending callback response: %s", err)
                    }
                    continue
                }

                // Send a confirmation message
                msg := tgbotapi.NewMessage(update.CallbackQuery.Message.Chat.ID, "Food entry deleted successfully!")
                bot.Send(msg)

                // Answer the callback query with an empty message to remove the keyboard
                callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
                if _, err := bot.Request(callbackConfig); err != nil {
                    log.Printf("Error sending callback response: %s", err)
                }
            } else if strings.HasPrefix(data, "previous:") || strings.HasPrefix(data, "next:") {
				offset, _ := strconv.Atoi(strings.Split(data, ":")[1])
				err := fetchFoodEntries(bot, update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.From.ID, db, offset, update.CallbackQuery.Message.MessageID)
				if err != nil {
					log.Printf("Failed to fetch food entries: %v", err)
				}
			} else {
                log.Printf("Unhandled callback data: %s", update.CallbackQuery.Data)
                callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Unhandled callback data")
                if _, err := bot.Request(callbackConfig); err != nil {
                    log.Printf("Error sending callback response: %s", err)
                }
            }
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