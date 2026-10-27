.POSIX:

PROJ = gui_stream
SRC = gui.c
OBJ = gui.o

CFLAGS = -std=c99 -O2 -Wall -Wextra -pedantic -Wno-unused-function -I/usr/include -I..
LDFLAGS = -lSDL3 -lSDL3_image -lm -pthread

all: $(PROJ)

$(OBJ): $(SRC) ../protocol.h nuklear.h nuklear_sdl3_renderer.h
	$(CC) -c $(CFLAGS) -o $@ $(SRC)

$(PROJ): $(OBJ)
	$(CC) -o $@ $(OBJ) $(LDFLAGS)

clean:
	rm -f $(PROJ) $(OBJ)

.PHONY: all clean
