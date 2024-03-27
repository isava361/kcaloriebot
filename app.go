package main

import (
	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"log"
	"database/sql"
	_ "github.com/mattn/go-sqlite3"
	"strings"
	"strconv"
	"fmt"
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
            } else if strings.HasPrefix(update.CallbackQuery.Data, "previous:") || strings.HasPrefix(update.CallbackQuery.Data, "next:") {
				offset, _ := strconv.Atoi(strings.Split(update.CallbackQuery.Data, ":")[1])
				err := fetchFoodEntries(bot, update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.From.ID, db, offset, update.CallbackQuery.Message.MessageID)
				if err != nil {
					log.Printf("Failed to fetch food entries: %v", err)
				}
			} else if strings.HasPrefix(update.CallbackQuery.Data, "favorite_") {
				favoriteID, err := strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "favorite_"), 10, 64)
				if err != nil {
					log.Printf("Invalid favorite ID: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid favorite ID")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Retrieve the favorite product details from the database
				favorite, err := getFavoriteFood(favoriteID, db)
				if err != nil {
					log.Printf("Failed to retrieve favorite food: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Failed to retrieve favorite food")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Create the inline keyboard with options for the selected favorite
				keyboard := tgbotapi.NewInlineKeyboardMarkup(
					tgbotapi.NewInlineKeyboardRow(
						tgbotapi.NewInlineKeyboardButtonData("Amend", "amend_"+strconv.FormatInt(favoriteID, 10)),
						tgbotapi.NewInlineKeyboardButtonData("Delete", "favedelete_"+strconv.FormatInt(favoriteID, 10)),
					),
				)
			
				// Update the message with the selected favorite details and options
				editMsg := tgbotapi.NewEditMessageText(update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.Message.MessageID, fmt.Sprintf("Selected favorite: %s\nCalories: %.2f, Protein: %.2f, Fat: %.2f, Carbs: %.2f", favorite.Name, favorite.Calories, favorite.Protein.Float64, favorite.Fat.Float64, favorite.Carbs.Float64))
				editMsg.ReplyMarkup = &keyboard
				bot.Send(editMsg)
			
				// Answer the callback query
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
				}
			} else if strings.HasPrefix(update.CallbackQuery.Data, "amend_") {
				favoriteID, err := strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "amend_"), 10, 64)
				if err != nil {
					log.Printf("Invalid favorite ID: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid favorite ID")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Retrieve the favorite product details from the database
				_, err = getFavoriteFood(favoriteID, db)
				if err != nil {
					log.Printf("Failed to retrieve favorite food: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Failed to retrieve favorite food")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Ask the user what they want to amend
				msg := tgbotapi.NewMessage(update.CallbackQuery.Message.Chat.ID, "What do you want to amend?")
				amendOptions := tgbotapi.NewInlineKeyboardMarkup(
					tgbotapi.NewInlineKeyboardRow(
						tgbotapi.NewInlineKeyboardButtonData("Calories", "calories_amend_"+strconv.FormatInt(favoriteID, 10)),
						tgbotapi.NewInlineKeyboardButtonData("Protein", "protein_amend_"+strconv.FormatInt(favoriteID, 10)),
					),
					tgbotapi.NewInlineKeyboardRow(
						tgbotapi.NewInlineKeyboardButtonData("Fat", "fat_amend_"+strconv.FormatInt(favoriteID, 10)),
						tgbotapi.NewInlineKeyboardButtonData("Carbs", "carbs_amend_"+strconv.FormatInt(favoriteID, 10)),
					),
				)
				msg.ReplyMarkup = amendOptions
				bot.Send(msg)
			
				// Answer the callback query
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
				}
			} else if strings.HasPrefix(update.CallbackQuery.Data, "favedelete_") {
				favoriteID, err := strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "favedelete_"), 10, 64)
				if err != nil {
					log.Printf("Invalid favorite ID: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid favorite ID")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Retrieve the favorite product details from the database
				favorite, err := getFavoriteFood(favoriteID, db)
				if err != nil {
					log.Printf("Failed to retrieve favorite food: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Failed to retrieve favorite food")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Ask for confirmation before deleting the favorite
				confirmationText := fmt.Sprintf("Are you sure you want to delete the favorite: %s?", favorite.Name)
				confirmationKeyboard := tgbotapi.NewInlineKeyboardMarkup(
					tgbotapi.NewInlineKeyboardRow(
						tgbotapi.NewInlineKeyboardButtonData("Yes", "confirm_delete_"+strconv.FormatInt(favoriteID, 10)),
						tgbotapi.NewInlineKeyboardButtonData("No", "cancel_delete_"+strconv.FormatInt(favoriteID, 10)),
					),
				)
			
				editMsg := tgbotapi.NewEditMessageText(update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.Message.MessageID, confirmationText)
				editMsg.ReplyMarkup = &confirmationKeyboard
				bot.Send(editMsg)
			
				// Answer the callback query
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
				}
			} else if strings.HasPrefix(update.CallbackQuery.Data, "confirm_delete_") {
				favoriteID, err := strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "confirm_delete_"), 10, 64)
				if err != nil {
					log.Printf("Invalid favorite ID: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid favorite ID")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Delete the favorite product from the database
				err = deleteFavoriteFood(favoriteID, db)
				if err != nil {
					log.Printf("Failed to delete favorite food: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Failed to delete favorite food")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Update the message to confirm the deletion
				editMsg := tgbotapi.NewEditMessageText(update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.Message.MessageID, "Favorite product deleted successfully!")
				bot.Send(editMsg)
			
				// Answer the callback query
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
				}
			} else if strings.HasPrefix(update.CallbackQuery.Data, "cancel_delete_") {
				// Update the message to cancel the deletion
				editMsg := tgbotapi.NewEditMessageText(update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.Message.MessageID, "Deletion cancelled.")
				bot.Send(editMsg)
			
				// Answer the callback query
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
				}			
			} else if strings.HasPrefix(update.CallbackQuery.Data, "calories_amend_") || strings.HasPrefix(update.CallbackQuery.Data, "protein_amend_") || strings.HasPrefix(update.CallbackQuery.Data, "fat_amend_") || strings.HasPrefix(update.CallbackQuery.Data, "carbs_amend_") {
				var favoriteID int64				
				parts := strings.Split(update.CallbackQuery.Data, "_")
				if len(parts) != 3 {
					log.Printf("Invalid callback data format: %s", update.CallbackQuery.Data)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid callback data format")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
				
				if strings.HasPrefix(update.CallbackQuery.Data, "calories_amend_") {
					favoriteID, err = strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "calories_amend_"), 10, 64)
					if err != nil {
						log.Printf("Invalid favorite ID: %s", err)
						callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid favorite ID")
						if _, err := bot.Request(callbackConfig); err != nil {
							log.Printf("Error sending callback response: %s", err)
						}
						continue
					}			
				} else if strings.HasPrefix(update.CallbackQuery.Data, "protein_amend_") {
					favoriteID, err = strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "protein_amend_"), 10, 64)
					if err != nil {
						log.Printf("Invalid favorite ID: %s", err)
						callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid favorite ID")
						if _, err := bot.Request(callbackConfig); err != nil {
							log.Printf("Error sending callback response: %s", err)
						}
						continue
					}			
				} else if strings.HasPrefix(update.CallbackQuery.Data, "fat_amend_") {
					favoriteID, err = strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "fat_amend_"), 10, 64)
					if err != nil {
						log.Printf("Invalid favorite ID: %s", err)
						callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid favorite ID")
						if _, err := bot.Request(callbackConfig); err != nil {
							log.Printf("Error sending callback response: %s", err)
						}
						continue
					}			
				} else if strings.HasPrefix(update.CallbackQuery.Data, "carbs_amend_") {
					favoriteID, err = strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "carbs_amend_"), 10, 64)
					if err != nil {
						log.Printf("Invalid favorite ID: %s", err)
						callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid favorite ID")
						if _, err := bot.Request(callbackConfig); err != nil {
							log.Printf("Error sending callback response: %s", err)
						}
						continue
					}			
				}
				// Ask the user to enter the new value for the selected nutrient
				var nutrient string
				if strings.HasPrefix(update.CallbackQuery.Data, "calories_amend_") {
					nutrient = "calories"
				} else if strings.HasPrefix(update.CallbackQuery.Data, "protein_amend_") {
					nutrient = "protein"
				} else if strings.HasPrefix(update.CallbackQuery.Data, "fat_amend_") {
					nutrient = "fat"
				} else if strings.HasPrefix(update.CallbackQuery.Data, "carbs_amend_") {
					nutrient = "carbs"
				}
			
				msg := tgbotapi.NewMessage(update.CallbackQuery.Message.Chat.ID, fmt.Sprintf("Enter the new value for %s:", nutrient))
				bot.Send(msg)
			
				// Store the selected favorite product and nutrient in the user's state
				setUserState(update.CallbackQuery.From.ID, stateWaitingForFavoriteAmendment, db)
				userFavorites[update.CallbackQuery.From.ID] = FavoriteFood{FavoriteID: favoriteID}
				userFavoriteNutrients[update.CallbackQuery.From.ID] = nutrient
			
				// Answer the callback query
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
				}
			} else if strings.HasPrefix(update.CallbackQuery.Data, "previous_fav:") || strings.HasPrefix(update.CallbackQuery.Data, "next_fav:") {
				offset, _ := strconv.Atoi(strings.Split(update.CallbackQuery.Data, ":")[1])
				err := fetchFavoriteFoods(bot, update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.From.ID, db, offset, update.CallbackQuery.Message.MessageID)
				if err != nil {
					log.Printf("Failed to fetch favorite foods: %v", err)
				}
			} else if strings.HasPrefix(update.CallbackQuery.Data, "entry_delete_") {
				entryID, err := strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "entry_delete_"), 10, 64)
				if err != nil {
					log.Printf("Invalid food entry ID: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid food entry ID")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Ask for confirmation before deleting the food entry
				confirmationText := "Are you sure you want to delete this food entry?"
				confirmationKeyboard := tgbotapi.NewInlineKeyboardMarkup(
					tgbotapi.NewInlineKeyboardRow(
						tgbotapi.NewInlineKeyboardButtonData("Yes", "confirm_delete_entry_"+strconv.FormatInt(entryID, 10)),
						tgbotapi.NewInlineKeyboardButtonData("No", "cancel_delete_entry_"+strconv.FormatInt(entryID, 10)),
					),
				)
			
				editMsg := tgbotapi.NewEditMessageText(update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.Message.MessageID, confirmationText)
				editMsg.ReplyMarkup = &confirmationKeyboard
				bot.Send(editMsg)
			
				// Answer the callback query
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
				}
			} else if strings.HasPrefix(update.CallbackQuery.Data, "confirm_delete_entry_") {
				entryID, err := strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "confirm_delete_entry_"), 10, 64)
				if err != nil {
					log.Printf("Invalid food entry ID: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid food entry ID")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Delete the food entry from the database
				err = deleteFoodEntry(entryID, db)
				if err != nil {
					log.Printf("Failed to delete food entry: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Failed to delete food entry")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Update the message to confirm the deletion
				editMsg := tgbotapi.NewEditMessageText(update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.Message.MessageID, "Food entry deleted successfully!")
				bot.Send(editMsg)
			
				// Answer the callback query
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
				}
			} else if strings.HasPrefix(update.CallbackQuery.Data, "cancel_delete_entry_") {
				// Update the message to cancel the deletion
				editMsg := tgbotapi.NewEditMessageText(update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.Message.MessageID, "Deletion cancelled.")
				bot.Send(editMsg)
			
				// Answer the callback query
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
				}
			} else if strings.HasPrefix(update.CallbackQuery.Data, "entry_choose_") {
				entryID, err := strconv.ParseInt(strings.TrimPrefix(update.CallbackQuery.Data, "entry_choose_"), 10, 64)
				if err != nil {
					log.Printf("Invalid entry ID: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Invalid entry ID")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Retrieve the entry details from the database
				entry, err := getEntry(entryID, db)
				if err != nil {
					log.Printf("Failed to retrieve entry: %s", err)
					callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "Failed to retrieve entry")
					if _, err := bot.Request(callbackConfig); err != nil {
						log.Printf("Error sending callback response: %s", err)
					}
					continue
				}
			
				// Create the inline keyboard with options for the selected entry
				keyboard := tgbotapi.NewInlineKeyboardMarkup(
					tgbotapi.NewInlineKeyboardRow(
						tgbotapi.NewInlineKeyboardButtonData("Delete", "entry_delete_"+strconv.FormatInt(entryID, 10)),
					),
				)
			
				// Update the message with the selected favorite details and options
				editMsg := tgbotapi.NewEditMessageText(update.CallbackQuery.Message.Chat.ID, update.CallbackQuery.Message.MessageID, fmt.Sprintf("Selected entry: %s\nCalories: %.2f, Protein: %.2f, Fat: %.2f, Carbs: %.2f", entry.Name.String, entry.Calories, entry.Protein.Float64, entry.Fat.Float64, entry.Carbs.Float64))
				editMsg.ReplyMarkup = &keyboard
				bot.Send(editMsg)
			
				// Answer the callback query
				callbackConfig := tgbotapi.NewCallback(update.CallbackQuery.ID, "")
				if _, err := bot.Request(callbackConfig); err != nil {
					log.Printf("Error sending callback response: %s", err)
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
			state INTEGER NOT NULL,
			timezone TEXT
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
			name TEXT,
			FOREIGN KEY (user_id) REFERENCES users (user_id)
		);

		CREATE TABLE IF NOT EXISTS favorite_foods (
			favorite_id INTEGER PRIMARY KEY AUTOINCREMENT,
			user_id INTEGER NOT NULL,
			name TEXT NOT NULL,
			calories REAL,
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