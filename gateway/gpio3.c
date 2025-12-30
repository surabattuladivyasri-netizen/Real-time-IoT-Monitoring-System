#include <wiringPi.h>
#include <stdio.h>
#include <stdlib.h>

// Matches the channels your Python script expects
int valid_channels[] = {0, 2, 3, 7, 12, 13, 14, 15, 16};
int num_valid_channels = sizeof(valid_channels) / sizeof(valid_channels[0]);

int main() {
    if (wiringPiSetup() == -1) {
        return 1;
    }

    // Initialize Pins with Pull-Ups
    for (int i = 0; i < num_valid_channels; i++) {
        int pin = valid_channels[i];
        pinMode(pin, INPUT);
        pullUpDnControl(pin, PUD_UP);
    }

    while (1) {
        for (int i = 0; i < num_valid_channels; i++) {
            int value = digitalRead(valid_channels[i]);
            // Print only the values separated by commas for Python to read
            printf("%d%s", value, (i == num_valid_channels - 1) ? "" : ",");
        }
        printf("\n"); // New line for each reading
        fflush(stdout);
        delay(100);  
    }
    return 0;
}