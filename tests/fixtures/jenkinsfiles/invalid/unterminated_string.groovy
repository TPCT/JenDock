// expect: line 7: unterminated string
pipeline {
    agent any
    stages {
        stage('A') {
            steps {
                echo 'oops
            }
        }
    }
}
