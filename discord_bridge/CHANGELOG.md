# Changelog

## 0.2.0

- Buttons can open a text form (Discord pop-up). The click fires the button's
  ID; submitting fires a separate event with the form's ID and the entered text
  in `values.message`.
- The log names who triggered each action: the Home Assistant user, the
  automation or script, and the Discord user whose click started it. Editor
  sends and updates log the Home Assistant user. Failed actions are logged,
  and Discord errors include the channel ID. Update the integration too.

## 0.1.0

- First version: button message sets with editor, sent-message history, and
  Home Assistant event/actions through the companion integration.
