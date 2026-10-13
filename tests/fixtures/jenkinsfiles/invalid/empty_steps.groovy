// expect: line 5: steps section is empty
pipeline {
    agent any
    stages {
        stage('A') { steps { } }
    }
}
