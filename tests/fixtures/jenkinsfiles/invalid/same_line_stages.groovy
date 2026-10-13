// expect: line 4: expected a new line or ';'
pipeline {
    agent any
    stages { stage('A') { steps { echo 'x' } } stage('B') { steps { echo 'y' } } }
}
